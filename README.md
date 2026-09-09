# Stardew Planner Skill

根据参考图片或文字需求，规划、复刻和调整《星露谷物语》原版布局，生成可编辑的配置、渲染图和实施简图，供玩家在游戏中照图布置。可在 Codex、Claude Code 等支持 Agent Skills 的助手中使用。

欢迎阅读[我的博客](https://codming.com/posts/stardew-planner-skill/)，了解更多关于此 skill 的内容。

## 使用 skill

### 安装与使用

运行需要 **Python 3.12 和 uv**，安装需要 **Node.js 22+ 和 Git**：

```bash
npx skills add sigming/stardew-planner-skill -a codex -a claude-code
```

支持以下 **26 种原版地图配置**：

| 类别 | 地图 |
| --- | --- |
| 农场 | 标准、河流、森林、山顶、荒野、四角、海滩、草原 |
| 室内 | 农舍及升级版本、地窖、棚屋及升级版本、鸡舍及升级版本、畜棚及升级版本、温室、农场洞穴、史莱姆屋 |
| 姜岛 | 姜岛农场、姜岛农舍内部 |

室内使用默认房间结构，暂不支持配偶房间、自定义农舍装修组合或 Mod 地图。

安装后，在对话中指定使用 `stardew-planner-skill`，提供图片或需求。例如：

- **按图复刻**：按照这张参考图复刻标准农场，保留建筑、道路、设备和装饰的位置。
- **新建规划**：规划草原农场，安排两座鸡舍、一片果园和自动浇水的种植区，保留温室。
- **规划完善**：检查当前方案的洒水器和稻草人覆盖，补充道路，调整拥挤的设施。
- **局部调整**：继续修改之前的 `plan.json`，把果园移到左下角，保持其余区域不变。
- **室内布置**：在大棚屋中安排小桶和箱子，铺设木地板，并留出操作通道。

普通作物统一用草莓示意，巨型作物用巨型南瓜示意；占地和位置保留。作用范围检查基于几何范围，未完整模拟季节生产、海滩浇水等游戏条件。

### 输出目录

默认交付到当前工作目录下的 `stardew-layout/`：

```text
stardew-layout/
├── plan.json              # 可继续编辑的布局配置
├── preview.png            # 地图与物品渲染图
├── implementation.png     # 占地、编号、坐标和物品图例
└── working/               # 摆放、作用范围、检查结果和文件校验数据
```

后续可将 `plan.json` 交给助手继续调整。规划过程的进度记录、参考图和 checkpoint 保存在方案工作目录中。

## 开发

```text
uv run --python 3.12 skills/stardew-planner-skill/scripts/layout.py --help

usage: layout.py [-h]
                 {maps,status,note,crop-image,check-review,merge-review,review-region,review-overall,calibrate,locate,new,generate,add,update,remove,apply,set,validate,coverage,render,import,checkpoint,deliver}
                 ...

Create, edit, validate and render Stardew Planner layouts locally.

positional arguments:
  {maps,status,note,crop-image,check-review,merge-review,review-region,review-overall,calibrate,locate,new,generate,add,update,remove,apply,set,validate,coverage,render,import,checkpoint,deliver}
    maps                List locally supported maps
    status              Read saved progress and inspect unfinished checks
                        after resuming.
    note                Save the current stage, decisions and next action.
    crop-image          Extract a bounded region from a local image.
    check-review        Verify every final image patch was reviewed
    merge-review        Combine independent patch review results
    review-region       Record checks for one inspected region.
    review-overall      Record the main agent overall inspection.
    calibrate           Fit screenshot coordinates from fixed terrain
                        landmarks
    locate              Convert a calibrated reference pixel to footprint
                        coordinates
    new                 Create a plan with explicit default buildings
    generate            Place recipe modules in valid terrain
    import              Convert an existing local planner JSON into a plan
    checkpoint          Save the current plan and previews for visual
                        comparison at one layout stage
    deliver             Generate the complete local delivery from one plan

options:
  -h, --help            show this help message and exit
```

## 版权与 License

Copyright (c) 2026 sigming。本项目原创代码、提示词和文档采用 [MIT License](LICENSE)。

游戏图像、地图及其他第三方内容不属于上述 MIT 授权范围，版权与使用条件依原作者规定。游戏素材来自《星露谷物语》（ConcernedApe）；数据与参考素材来自 Stardew Planner（Henrik Peinar）、stardewplan.com 和星露谷 Wiki，具体来源保存在 `skills/stardew-planner-skill/data/` 的记录中。

本项目为非官方工具，与游戏作者及上述站点无官方关联。
