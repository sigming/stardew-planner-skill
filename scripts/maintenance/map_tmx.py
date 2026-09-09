"""Read native 16-pixel Stardew TMX maps without executing website code."""

import base64
import copy
import gzip
import struct
import xml.etree.ElementTree as ET
import zlib

from PIL import Image

GID_MASK = 0x1FFFFFFF
FLIP_H = 0x80000000
FLIP_V = 0x40000000
FLIP_D = 0x20000000


def properties(element):
    return {
        entry.get('name'): entry.get('value', entry.text or '')
        for entry in element.findall('properties/property')
    }


def truth(value):
    return bool(value) and str(value).lower() not in ['false', 'f']


def decode_layer(element):
    encoding = element.get('encoding', 'csv')
    value = element.text or ''
    if encoding == 'csv':
        return [int(part.strip()) for part in value.split(',') if part.strip()]
    if encoding != 'base64':
        raise ValueError(f'Unsupported TMX encoding: {encoding}')
    blob = base64.b64decode(value.strip(), validate=True)
    compression = element.get('compression', '')
    if compression == 'zlib':
        blob = zlib.decompress(blob)
    elif compression == 'gzip':
        blob = gzip.decompress(blob)
    elif compression:
        raise ValueError(f'Unsupported TMX compression: {compression}')
    return list(struct.unpack(f'<{len(blob) // 4}I', blob))


class TmxMap:
    def __init__(self, blob):
        root = ET.fromstring(blob)
        self.width = int(root.get('width'))
        self.height = int(root.get('height'))
        if (root.get('tilewidth'), root.get('tileheight')) != ('16', '16'):
            raise ValueError('Only native 16-pixel TMX maps are supported')
        self.properties = properties(root)
        self.tilesets = []
        for element in root.findall('tileset'):
            source = element.find('image')
            if source is None:
                raise ValueError('External TMX tilesets are not supported')
            self.tilesets.append(
                {
                    'first_gid': int(element.get('firstgid')),
                    'count': int(element.get('tilecount')),
                    'columns': int(element.get('columns')),
                    'source': source.get('source'),
                    'properties': {
                        int(tile.get('id')): properties(tile)
                        for tile in element.findall('tile')
                    },
                }
            )
        self.layers = {}
        for layer in root.findall('layer'):
            if (int(layer.get('width')), int(layer.get('height'))) != (
                self.width,
                self.height,
            ):
                raise ValueError('TMX layer dimensions do not match the map')
            values = decode_layer(layer.find('data'))
            if len(values) != self.width * self.height:
                raise ValueError(
                    'TMX layer data length does not match the map'
                )
            self.layers[layer.get('name')] = values
        if 'Back' not in self.layers:
            raise ValueError('TMX map is missing its Back layer')
        self.tile_properties = {}
        for group in root.findall('objectgroup'):
            for obj in group.findall('object'):
                if obj.get('name') != 'TileData':
                    continue
                key = (
                    group.get('name'),
                    int(float(obj.get('x')) // 16),
                    int(float(obj.get('y')) // 16),
                )
                self.tile_properties.setdefault(key, {}).update(
                    properties(obj)
                )

    def tile(self, gid):
        index = gid & GID_MASK
        if not index:
            return None, None
        for sheet in reversed(self.tilesets):
            if sheet['first_gid'] <= index:
                return sheet, index - sheet['first_gid']
        raise ValueError(f'Unknown TMX tile: {gid}')

    def props(self, layer, x, y):
        values = self.layers.get(layer)
        gid = values[y * self.width + x] if values else 0
        sheet, index = self.tile(gid)
        inherited = sheet['properties'].get(index, {}) if sheet else {}
        return {**inherited, **self.tile_properties.get((layer, x, y), {})}

    def gid(self, layer, x, y):
        values = self.layers.get(layer)
        return values[y * self.width + x] if values else 0

    def paste(self, patch, rect, dest_x, dest_y):
        """Apply the same ordered-layer patch semantics used by the game map."""
        if self.width < dest_x + rect[2] or self.height < dest_y + rect[3]:
            raise ValueError('Map patch extends beyond the base map')
        offsets = {}
        for sheet in patch.tilesets:
            match = next(
                (s for s in self.tilesets if s['source'] == sheet['source']),
                None,
            )
            if match is None:
                match = copy.deepcopy(sheet)
                match['first_gid'] = max(
                    s['first_gid'] + s['count'] for s in self.tilesets
                )
                self.tilesets.append(match)
            offsets[sheet['first_gid']] = (
                match['first_gid'] - sheet['first_gid']
            )
        for name in patch.layers:
            self.layers.setdefault(name, [0] * (self.width * self.height))
        left, top, width, height = rect
        for dy in range(height):
            for dx in range(width):
                active = False
                for name, values in patch.layers.items():
                    gid = values[(top + dy) * patch.width + left + dx]
                    if not gid and not active:
                        continue
                    active = True
                    if gid:
                        sheet, _ = patch.tile(gid)
                        flags = gid & ~GID_MASK
                        gid = (
                            (gid & GID_MASK) + offsets[sheet['first_gid']]
                        ) | flags
                    self.layers[name][
                        (dest_y + dy) * self.width + dest_x + dx
                    ] = gid
        for (name, x, y), value in patch.tile_properties.items():
            if left <= x < left + width and top <= y < top + height:
                self.tile_properties.setdefault(
                    (name, x - left + dest_x, y - top + dest_y), {}
                ).update(value)
        self.tilesets.sort(key=lambda sheet: sheet['first_gid'])

    def render(self, sheets):
        canvas = Image.new(
            'RGBA', (self.width * 16, self.height * 16), '#171714'
        )
        sprites = {}
        for name, values in self.layers.items():
            if name == 'Paths':
                continue
            for index, gid in enumerate(values):
                if not gid:
                    continue
                if gid not in sprites:
                    sheet, local_id = self.tile(gid)
                    x = local_id % sheet['columns'] * 16
                    y = local_id // sheet['columns'] * 16
                    image = sheets[sheet['source']]
                    if x + 16 > image.width or y + 16 > image.height:
                        raise ValueError(
                            f'Tile outside image: {sheet["source"]} {local_id}'
                        )
                    sprite = image.crop((x, y, x + 16, y + 16))
                    if gid & FLIP_D:
                        sprite = sprite.transpose(Image.Transpose.TRANSPOSE)
                    if gid & FLIP_H:
                        sprite = sprite.transpose(
                            Image.Transpose.FLIP_LEFT_RIGHT
                        )
                    if gid & FLIP_V:
                        sprite = sprite.transpose(
                            Image.Transpose.FLIP_TOP_BOTTOM
                        )
                    sprites[gid] = sprite
                canvas.alpha_composite(
                    sprites[gid],
                    (index % self.width * 16, index // self.width * 16),
                )
        return canvas

    def warps(self):
        values = self.properties.get('Warp', '').split()
        if len(values) % 5:
            raise ValueError('Malformed TMX Warp property')
        result = []
        for start in range(0, len(values), 5):
            x, y = map(int, values[start : start + 2])
            destination = values[start + 2]
            if 0 <= x < self.width and 0 <= y < self.height:
                result.append((x, y, destination))
            elif -1 <= x <= self.width and -1 <= y <= self.height:
                result.append(
                    (
                        min(max(x, 0), self.width - 1),
                        min(max(y, 0), self.height - 1),
                        destination,
                    )
                )
        return result

    def interaction_tiles(self):
        result = set(self.warps())
        for (layer, x, y), values in self.tile_properties.items():
            action = values.get('TouchAction') or values.get('Action', '')
            words = action.split()
            if not words or words[0] not in ['Warp', 'FarmObelisk']:
                continue
            destination = words[1] if 'TouchAction' in values else words[-1]
            row = y + 1 if layer.startswith('Buildings') else y
            if 0 <= x < self.width and 0 <= row < self.height:
                result.add((x, row, destination))
        return sorted(result)

    def restrictions(self, category, greenhouse=False):
        indoor = category in ['interior', 'coop', 'barn']
        blocked = {
            key: set()
            for key in ['accessible', 'buildable', 'tillable', 'furniture']
        }
        wall_tiles = set()
        if indoor:
            for (layer, x, y), props in self.tile_properties.items():
                if layer == 'Back' and 'WallID' in props:
                    wall_tiles.update(
                        (x, row) for row in range(y, min(y + 3, self.height))
                    )
        for y in range(self.height):
            for x in range(self.width):
                cell = (x, y)
                back = self.gid('Back', x, y)
                building = self.gid('Buildings', x, y)
                props = self.props('Back', x, y)
                front_props = self.props('Buildings', x, y)
                water = truth(props.get('Water'))
                solid = building and not any(
                    key in front_props for key in ['Passable', 'Shadow']
                )
                accessible = (
                    bool(back)
                    and not water
                    and not solid
                    and 'Passable' not in props
                )
                furniture = (
                    accessible
                    and 'NoFurniture' not in props
                    and cell not in wall_tiles
                )
                buildable = (
                    furniture
                    if indoor
                    else (
                        bool(back)
                        and not building
                        and not water
                        and (
                            'Buildable' not in props
                            or truth(props['Buildable'])
                        )
                        and (
                            truth(props.get('Buildable'))
                            or truth(props.get('Diggable'))
                        )
                    )
                )
                tillable = (
                    accessible
                    and not building
                    and truth(props.get('Diggable'))
                )
                if indoor and not greenhouse:
                    tillable = False
                if category == 'island' and tillable:
                    sheet, _ = self.tile(back)
                    tillable = 'outdoorsTileSheet' in sheet['source']
                for key, allowed in [
                    ('accessible', accessible),
                    ('furniture', furniture),
                    ('buildable', buildable),
                    ('tillable', tillable),
                ]:
                    if not allowed:
                        blocked[key].add(cell)
        for x, y, _ in self.interaction_tiles():
            for key in ['buildable', 'furniture', 'tillable']:
                blocked[key].add((x, y))
        blocked['wall'] = {
            (x, y) for y in range(self.height) for x in range(self.width)
        } - wall_tiles
        return blocked, wall_tiles
