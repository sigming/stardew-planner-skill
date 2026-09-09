"""Map-specific placement rules from the explicitly supported vanilla scope."""

INDOOR_CATEGORIES = {'interior', 'coop', 'barn'}
WALL_FURNITURE_TYPES = {'painting', 'window', 'sconce'}


def furniture_type(metadata):
    """Prefer the native Furniture data category when it is available."""
    return metadata.get('furniture_type') or metadata.get(
        'game_furniture_type'
    )


def item_scope_error(map_info, metadata):
    rules = map_info.get('gameplay_rules', {})
    if (
        metadata.get('placement_notes', {}).get('requires_outdoors')
        and map_info.get('category') != 'farm'
    ):
        return 'outdoor_equipment_requires_outdoors'
    if metadata.get('category') == 'building' and not rules.get(
        'allow_farm_buildings', map_info.get('category', 'farm') == 'farm'
    ):
        return 'farm_building_outside_farm'
    if metadata.get('category') == 'crop':
        if metadata.get('subcategory') == 'giant-crops':
            if not rules.get('allow_giant_crops', True):
                return 'giant_crop_not_supported_on_map'
        elif not rules.get('allow_ground_crops', True):
            return 'ground_crop_not_supported_on_map'
    if metadata.get('subcategory') == 'tree' and not rules.get(
        'allow_trees', True
    ):
        return 'tree_not_supported_on_map'
    if (
        furniture_type(metadata) in WALL_FURNITURE_TYPES
        and map_info.get('category', 'farm') not in INDOOR_CATEGORIES
    ):
        return 'wall_furniture_requires_interior'
    return None


def placement_layer(map_info, metadata):
    if metadata.get('id') == 'crab-pot':
        return 'shore_water'
    if furniture_type(metadata) in WALL_FURNITURE_TYPES:
        return 'wall'
    if map_info.get('category') in INDOOR_CATEGORIES and metadata.get(
        'category'
    ) in ['furniture', 'craftable']:
        return 'furniture'
    return metadata['placement']['restriction_layer'] or 'accessible'
