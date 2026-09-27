# my-airpage-push

用 GitHub Actions 定时把 AirPage 模板推到墨水屏。任务只在 GitHub 上运行。

当前接入的是网站上的迷宫模板。默认每天北京时间 08:00 推一张新迷宫。

## 配置

改 `config.json` 即可。GitHub Actions Variables 里的同名设置会覆盖文件。

| 配置 | 环境变量 | 当前值 | 说明 |
| --- | --- | --- | --- |
| `template` | `AIRPAGE_TEMPLATE` | `maze` | 目前只能填 `maze` |
| `maze.seed` | `AIRPAGE_MAZE_SEED` | `daily` | `daily` 用当天北京日期做种子；填其他文字则每天同一张迷宫 |
| `maze.title` | `AIRPAGE_MAZE_TITLE` | `MAZE · 迷宫` | 左上角标题 |
| `maze.solve` | `AIRPAGE_MAZE_SOLVE` | `false` | `true` 时画出灰色解答路径 |
| `maze.large` | `AIRPAGE_MAZE_LARGE` | `false` | `true` 时使用更大网格 |
| `width` / `height` | `AIRPAGE_WIDTH` / `AIRPAGE_HEIGHT` | `480` / `800` | 画面尺寸 |
| `mode` | `AIRPAGE_MODE` | `gray4` | `gray4` 或 `gray16` |
| `origin` | `AIRPAGE_ORIGIN` | `https://airpage.crossmux.cn` | 只接受 AirPage 官方域名 |

设备 ID 不要写进这个文件。它只放在 GitHub Secret `AIRPAGE_DEVICE_ID`。

## 推送前在 GitHub 上设置

1. 把本仓库推到 `main`。
2. Settings → Secrets and variables → Actions → New repository secret。
3. Name 填 `AIRPAGE_DEVICE_ID`，Value 填设备链接里的 `id` 参数。

## 什么时候运行

- 每天北京时间 08:00 自动运行一次。时间写在 `.github/workflows/airpage-push.yml` 的 cron：`0 0 * * *`（UTC）。
- 也可以在 Actions 页面手动运行「AirPage 定时推送」。

定时任务要等工作流出现在默认分支 `main` 之后才会生效。
