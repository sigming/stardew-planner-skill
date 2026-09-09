# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

"""Refresh palm sprites and references for entities omitted from layouts."""

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from audit_catalog import digest, download, write_json
from PIL import Image

ROOT = Path(__file__).resolve().parents[2] / 'skills/stardew-planner-skill'
WIKI = 'https://stardewvalleywiki.com'
PETS = {
    **{
        f'pet-cat-{n}': (f'Cat_{n}.png', f'猫（款式 {n}）', f'Cat {n}')
        for n in range(1, 6)
    },
    **{
        f'pet-dog-{n}': (f'Dog_{n}.png', f'狗（款式 {n}）', f'Dog {n}')
        for n in range(1, 6)
    },
    'pet-turtle': ('Turtle.png', '乌龟', 'Turtle'),
    'pet-iridium-turtle': ('Iridium_Turtle.png', '铱金乌龟', 'Iridium Turtle'),
    'horse': ('Horse.png', '马匹', 'Horse'),
}
ANIMALS = {
    'animal-chicken': ('2/22/White_Chicken.png', '鸡（白色示例）', 'Chicken'),
    'animal-duck': ('3/38/Duck.png', '鸭', 'Duck'),
    'animal-dinosaur': ('8/89/Dinosaur.png', '恐龙（蜥蜴）', 'Dinosaur'),
    'animal-rabbit': ('f/fd/Rabbit.png', '兔子', 'Rabbit'),
    'animal-cow': ('3/36/White_Cow.png', '牛（白色示例）', 'Cow'),
    'animal-ostrich': ('a/ad/Ostrich.png', '鸵鸟', 'Ostrich'),
    'animal-goat': ('5/52/Goat.png', '山羊', 'Goat'),
    'animal-sheep': ('c/cc/Sheep.png', '绵羊', 'Sheep'),
    'animal-pig': ('3/30/Pig.png', '猪', 'Pig'),
    'wild-butterfly': ('6/6c/ButterflyAnimated.gif', '蝴蝶', 'Butterfly'),
    'wild-crow': ('f/fa/Crow.png', '乌鸦', 'Crow'),
    'wild-owl': ('5/5c/Owl.png', '猫头鹰黑影', 'Owl silhouette'),
    'wild-seagull': ('9/9a/Seagull.png', '海鸥', 'Seagull'),
    'moving-slime': ('2/27/Purple_Slime.png', '史莱姆（紫色示例）', 'Slime'),
    'truffle': ('f/f2/Truffle.png', '松露（猪的产物）', 'Truffle'),
}
FAIRY_URL = f'{WIKI}/mediawiki/images/1/12/Fairy_Box_Colors.png'
FROG_URL = f'{WIKI}/mediawiki/images/6/66/Frog_Egg_Colors.png'
PARROT_URL = f'{WIKI}/mediawiki/images/f/ff/Golden_Joja_Parrot.jpg'
MUSHROOM_LOG_READY_URL = f'{WIKI}/mediawiki/images/8/83/Mushroom_Log_Ready.png'
PALMS = {
    'palm-tree': ('6', 'tree_palm', '棕榈树（沙漠）', 'Palm Tree (Desert)'),
    'palm-tree-island': (
        '9',
        'tree_palm2',
        '棕榈树（姜岛）',
        'Palm Tree (Island)',
    ),
}


def save_image(root, relative, pixels, source, url, **metadata):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels.save(path)
    return {
        'path': relative,
        'url': url,
        'source_sha256': digest(source),
        'sha256': digest(path.read_bytes()),
        'size_px': list(pixels.size),
        **metadata,
    }


def entity_specs(pet_urls, version='1.6.15'):
    specs = {
        item_id: {
            'url': pet_urls[filename],
            'name_zh': name_zh,
            'name_en': name_en,
            'source_page': f'{WIKI}/Animals',
            'aliases': ['宠物', 'pet']
            if item_id != 'horse'
            else ['马', '马匹'],
        }
        for item_id, (filename, name_zh, name_en) in PETS.items()
    }
    for item_id, (image_path, name_zh, name_en) in ANIMALS.items():
        specs[item_id] = {
            'url': f'{WIKI}/mediawiki/images/{image_path}',
            'name_zh': name_zh,
            'name_en': name_en,
            'source_page': f'{WIKI}/Animals',
            'aliases': ['蜥蜴'] if item_id == 'animal-dinosaur' else [],
        }
        if item_id.startswith('animal-') or item_id == 'moving-slime':
            specs[item_id]['note_zh'] = (
                '图中使用一种颜色示例，其他颜色同样忽略放置。'
            )
    specs['moving-slime']['source_page'] = f'{WIKI}/Slimes'
    specs['moving-slime']['aliases'] = ['史莱姆', 'Purple Slime']
    specs['truffle']['source_page'] = f'{WIKI}/Truffle'
    specs['truffle']['aliases'] = ['松露', '猪的松露']
    specs['truffle']['note_zh'] = (
        '忽略猪在畜棚外产生的松露，不计入布局占位或物品数量。'
    )
    specs['torch'] = {
        'url': f'{WIKI}/mediawiki/images/b/b2/Torch.png',
        'name_zh': '火把（地面或围栏上的小型光源）',
        'name_en': 'Torch',
        'source_page': 'https://zh.stardewvalleywiki.com/%E7%81%AB%E6%8A%8A',
        'source_table': 'Objects',
        'source_item_id': '93',
        'aliases': ['火把', '小型火把'],
        'note_zh': '仅在确认是小型火把（有时称小蜡烛）后忽略光源及火焰；附着在围栏上时保留围栏本身。木灯柱、铁灯柱、蜡烛灯、木头火炬和其他独立火炬均保留。木头火炬与木围栏可能在南北相邻格发生贴图重叠，需比较底座、接地格和围栏连接，不能仅因火焰位于围栏上方就判定为附着光源。“火炬”“蜡烛”“candle”等泛称不直接归类为忽略项；无法确认时记录待辨认问题。',
    }
    specs['mailbox'] = {
        'url': f'https://assets.stardewplan.com/assets/{version}/buildings/Mailbox.png',
        'name_zh': '信箱（农舍或联机小屋自带）',
        'name_en': 'Mailbox',
        'source_page': f'{WIKI}/Farmhouse',
        'aliases': ['信箱', '邮箱', 'Mailbox'],
        'note_zh': '农舍和联机小屋自带信箱，忽略独立放置和计数；建筑原图中的信箱保留。勿识别为出货箱、灯具或未知物体。',
    }
    specs['toddler'] = {
        'url': f'{WIKI}/mediawiki/images/0/0c/ToddlerComposite.png',
        'name_zh': '幼儿（多种外观）',
        'name_en': 'Toddler',
        'source_page': f'{WIKI}/Children',
        'aliases': [
            '幼儿',
            '小孩',
            '孩子',
            '儿童',
            'toddler',
            'child',
            'children',
        ],
        'reference_composite': True,
        'note_zh': '图中四种幼儿外观均属于人物，仅供识别；复刻布局时忽略，不放置、不计数，也不以家具或玩偶替代。',
    }
    specs['wild-butterfly']['source_frame'] = 0
    specs['wild-owl']['background_removal'] = {
        'method': 'owl_silhouette',
        'maximum_channel': 55,
    }
    specs['wild-seagull']['source_rect_px'] = [16, 30, 105, 85]
    specs['wild-seagull']['background_removal'] = {
        'method': 'cyan_water',
        'green_over_red': 20,
        'blue_over_red': 25,
    }
    specs['fairy-companion-1'] = {
        'url': FAIRY_URL,
        'name_zh': '仙女（多色，形状相同）',
        'name_en': 'Fairy (same shape, many colors)',
        'source_page': f'{WIKI}/Fairy_Box',
        'source_rect_px': [0, 0, 48, 48],
        'aliases': [
            '仙女盒',
            'Fairy Box',
            *(f'fairy-companion-{number}' for number in range(2, 9)),
        ],
        'note_zh': '仙女盒召唤物有多种颜色，形状相同；图鉴保留一种代表外观，其他颜色同样忽略放置。',
    }
    specs['frog-companion-2'] = {
        'url': FROG_URL,
        'name_zh': '青蛙（多色，形状相同）',
        'name_en': 'Frog (same shape, many colors)',
        'source_page': 'https://zh.stardewvalleywiki.com/File:Frog_Egg_Colors.png',
        'source_rect_px': [70, 7, 106, 43],
        'background_removal': {
            'method': 'frog_screenshot_palette',
            'minimum_chroma': 36,
            'dark_channel_below': 50,
            'preserved_rgb': [],
            'palette_tolerance': 2,
        },
        'aliases': [
            '青蛙蛋',
            'Frog Egg',
            '五彩青蛙',
            *(
                f'frog-companion-{number}'
                for number in range(1, 15)
                if number != 2
            ),
        ],
        'note_zh': '青蛙蛋召唤物有多种颜色，形状相同；图鉴保留一种代表外观，其他颜色及五彩变色均忽略放置。',
    }
    specs['island-parrot-perch'] = {
        'url': PARROT_URL,
        'name_zh': '姜岛鹦鹉及支架',
        'name_en': 'Island parrot and perch',
        'source_page': f'{WIKI}/Golden_Joja_Parrot',
        'source_rect_px': [172, 273, 226, 424],
        'aliases': ['黄金Joja鹦鹉', 'Golden Joja Parrot', '鹦鹉架'],
        'note_zh': '从用户提供的截图裁出鹦鹉及支架；两者均无需额外放置，保留地图背景中的固定景物。',
    }
    return specs


def extract_entity(source, spec):
    with Image.open(BytesIO(source)) as image:
        image.seek(spec.get('source_frame', 0))
        original = image.convert('RGBA')
    rect = spec.get('source_rect_px', [0, 0, *original.size])
    left, top, right, bottom = rect
    if not (
        0 <= left < right <= original.width
        and 0 <= top < bottom <= original.height
    ):
        raise ValueError(f'Reference crop outside source: {spec["url"]}')
    pixels = original.crop(rect)
    removal = spec.get('background_removal', {})
    if removal.get('method') == 'frog_screenshot_palette':
        # These bounds separate the supplied screenshot's gray flooring and
        # shadows from frog colors. Preserve the void frog's brown body too.
        pixels.putdata(
            [
                pixel
                if max(pixel[:3]) - min(pixel[:3]) >= removal['minimum_chroma']
                or min(pixel[:3]) < removal['dark_channel_below']
                or any(
                    max(
                        abs(value - sample)
                        for value, sample in zip(pixel[:3], rgb, strict=True)
                    )
                    <= removal['palette_tolerance']
                    for rgb in removal['preserved_rgb']
                )
                else (0, 0, 0, 0)
                for pixel in pixels.get_flattened_data()
            ]
        )
    elif removal.get('method') == 'owl_silhouette':
        pixels.putdata(
            [
                pixel
                if max(pixel[:3]) <= removal['maximum_channel']
                else (0, 0, 0, 0)
                for pixel in pixels.get_flattened_data()
            ]
        )
    elif removal.get('method') == 'cyan_water':
        pixels.putdata(
            [
                (0, 0, 0, 0)
                if pixel[1] - pixel[0] > removal['green_over_red']
                or pixel[2] - pixel[0] > removal['blue_over_red']
                else pixel
                for pixel in pixels.get_flattened_data()
            ]
        )
    elif removal:
        raise ValueError(f'Unknown reference background removal: {removal}')
    return pixels, rect


def refresh_entities(root, supplemental):
    page_url = f'{WIKI}/Animals'
    page = download(page_url)
    paths = re.findall(
        r'/mediawiki/images/[a-f0-9]/[a-f0-9]{2}/[^"<> /]+\.png',
        page.decode(),
    )
    urls = {Path(path).name: WIKI + path for path in paths}
    specs = entity_specs(urls, supplemental['asset_version'])
    source_urls = sorted({spec['url'] for spec in specs.values()})
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = dict(
            zip(
                source_urls,
                pool.map(download, source_urls),
                strict=True,
            )
        )
    entities = {}
    for item_id, spec in specs.items():
        source = sources[spec['url']]
        pixels, rect = extract_entity(source, spec)
        asset = save_image(
            root,
            f'assets/recognition/entities/{item_id}.png',
            pixels,
            source,
            spec['url'],
            source_rect_px=rect,
            **{
                key: spec[key]
                for key in ['source_frame', 'background_removal']
                if key in spec
            },
        )
        entities[item_id] = {
            'id': item_id,
            'name_zh': spec['name_zh'],
            'name_en': spec['name_en'],
            'aliases': spec['aliases'],
            'recognition_group': 'ignored_entities',
            'recognition_only': True,
            'placement_supported': False,
            'recognition_action': 'omit',
            'note_zh': spec.get(
                'note_zh', '仅用于识别，布局中忽略，不使用家具替代。'
            ),
            'source_page': spec['source_page'],
            **{
                key: spec[key]
                for key in [
                    'source_table',
                    'source_item_id',
                    'reference_composite',
                ]
                if key in spec
            },
            'sprites': {'default': asset},
        }
    supplemental['reference_entities'] = entities
    supplemental['sources']['recognition_animals'] = {
        'url': page_url,
        'sha256': digest(page),
    }


def refresh_reference_assets(root, supplemental, version):
    refresh_entities(root, supplemental)
    refresh_mushroom_log(root, supplemental)
    refresh_camping_tools(root, supplemental)
    tree_url = (
        f'https://assets.stardewplan.com/assets/{version}/data/WildTrees.json'
    )
    tree_source = download(tree_url)
    trees = json.loads(tree_source)
    for item_id, (tree_id, texture, name_zh, name_en) in PALMS.items():
        if (
            trees[tree_id]['Textures'][0]['Texture']
            != f'TerrainFeatures\\{texture}'
        ):
            raise ValueError(f'Palm texture changed: {tree_id}')
        url = f'https://assets.stardewplan.com/assets/{version}/terrain/{texture}.png'
        source = download(url)
        with Image.open(BytesIO(source)) as image:
            sheet = image.convert('RGBA')
        # The mature crown and stump are separate parts in WildTree textures.
        layers = [
            {'source_rect_px': [32, 96, 48, 128], 'offset_px': [16, 64]},
            {'source_rect_px': [0, 0, 48, 96], 'offset_px': [0, 0]},
        ]
        pixels = Image.new('RGBA', (48, 96))
        for layer in layers:
            pixels.alpha_composite(
                sheet.crop(layer['source_rect_px']), layer['offset_px']
            )
        asset = save_image(
            root,
            f'data/planner/images/{item_id}-default.png',
            pixels,
            source,
            url,
            source_rect_px=[0, 0, 48, 96],
            source_layers=layers,
        )
        supplemental['items'][item_id] = {
            'id': item_id,
            'template_type': 'oak-tree',
            'planner_type': item_id,
            'name_zh': name_zh,
            'name_en': name_en,
            'recognition_group': 'trees',
            'family_id': 'forest_trees',
            'source_page': f'{WIKI}/Palm_Tree',
            'source_item_id': tree_id,
            'source_table': 'WildTrees',
            'recognition_only': False,
            'footprint_tiles': [1, 1],
            'footprint_source': 'mature_tree_trunk',
            'sprites': {'default': asset},
        }
    supplemental['sources']['wild_trees'] = {
        'url': tree_url,
        'sha256': digest(tree_source),
    }
    return supplemental


def refresh_mushroom_log(root, supplemental):
    source = download(MUSHROOM_LOG_READY_URL)
    with Image.open(BytesIO(source)) as image:
        original = image.convert('RGBA')
    if original.size != (48, 96):
        raise ValueError(
            'Mushroom Log Ready source size changed; review the native sprite scale'
        )
    pixels = original.resize((16, 32), Image.Resampling.NEAREST)
    asset = save_image(
        root,
        'data/planner/images/mushroom-log-ready.png',
        pixels,
        source,
        MUSHROOM_LOG_READY_URL,
        source_rect_px=[0, 0, 48, 96],
        source_scale=3,
    )
    supplemental['items']['mushroom-log-ready'] = {
        'id': 'mushroom-log-ready',
        'template_type': 'mushroom-log',
        'planner_type': 'mushroom-log-ready',
        'parent_id': 'mushroom-log',
        'appearance_label': 'ready',
        'name_zh': '蘑菇树桩（长出蘑菇）',
        'name_en': 'Mushroom Log (Ready)',
        'recognition_group': 'tools',
        'family_id': 'tree_production',
        'source_page': f'{WIKI}/Mushroom_Log',
        'source_item_id': 'MushroomLogReady',
        'recognition_only': False,
        'footprint_tiles': [1, 1],
        'footprint_source': 'mushroom_log_same_object_ready_state',
        'sprites': {'default': asset},
    }


def refresh_camping_tools(root, supplemental):
    for item_id, name_zh, name_en, source_id, image_path, size, footprint in [
        (
            'cookout-kit',
            '野炊工具',
            'Cookout Kit',
            '926',
            '4/4e/Campfire_Cooker.png',
            (48, 96),
            [1, 1],
        ),
        (
            'tent-kit',
            '便携帐篷',
            'Tent Kit',
            'TentKit',
            '6/65/Tent_Kit_Deployed.png',
            (144, 177),
            [3, 2],
        ),
    ]:
        url = f'{WIKI}/mediawiki/images/{image_path}'
        source = download(url)
        with Image.open(BytesIO(source)) as image:
            original = image.convert('RGBA')
        if original.size != size:
            raise ValueError(
                f'{name_en} source changed; review its native scale'
            )
        pixels = original.resize(
            tuple(side // 3 for side in size), Image.Resampling.NEAREST
        )
        asset = save_image(
            root,
            f'data/planner/images/{item_id}-default.png',
            pixels,
            source,
            url,
            source_rect_px=[0, 0, *size],
            source_scale=3,
        )
        supplemental['items'][item_id] = {
            'id': item_id,
            'template_type': 'campfire',
            'planner_type': item_id,
            'name_zh': name_zh,
            'name_en': name_en,
            'recognition_group': 'tools',
            'family_id': 'camping',
            'source_page': f'{WIKI}/{name_en.replace(" ", "_")}',
            'source_table': 'Objects',
            'source_item_id': source_id,
            'recognition_only': False,
            'footprint_tiles': footprint,
            'footprint_source': 'deployed_equipment_and_wiki_usage',
            'placement_notes': {
                'temporary': True,
                'requires_outdoors': item_id == 'tent-kit',
                'note_zh': '临时野炊设备，放置后会在次日消失。'
                if item_id == 'cookout-kit'
                else '展开后占3×2格，仅可放在室外；使用或过夜后消失。',
            },
            'sprites': {'default': asset},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    subset = parser.add_mutually_exclusive_group()
    subset.add_argument('--entities-only', action='store_true')
    subset.add_argument('--mushroom-log-only', action='store_true')
    subset.add_argument('--camping-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    path = root / 'data/recognition/supplemental.json'
    supplemental = json.loads(path.read_text())
    if args.camping_only:
        refresh_camping_tools(root, supplemental)
    elif args.mushroom_log_only:
        refresh_mushroom_log(root, supplemental)
    elif args.entities_only:
        refresh_entities(root, supplemental)
    else:
        refresh_reference_assets(
            root, supplemental, supplemental['asset_version']
        )
    write_json(path, supplemental)
    print(
        json.dumps(
            {'ignored_entities': len(supplemental['reference_entities'])}
        )
    )


if __name__ == '__main__':
    main()
