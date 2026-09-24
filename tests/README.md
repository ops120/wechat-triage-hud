# tests — 可重复跑的验收

**这里没有单元测试，只有"验收"**：每个脚本都对着**真实产物**判定（真窗口、真鼠标坐标、真 OCR、
真 API 调用或真日志文件），并把观察到的数字打出来，而不是"看代码里有没有那行"。
理由见本机自用备忘录（不入库）：本项目最贵的几次翻车（自遮挡把面板自己的字读成会话名、
缓存键不含身份导致"填了不生效"）**都不是单元测试能抓的**，只有真机验收能抓。

## 跑法

```bash
# 环境（在仓库根）
pip install -r requirements.txt
cp .env.example .env        # 填 TypeSafe API Key
# 前提：PC 微信已登录，窗口可见且未被遮挡（面板按设计会拒绝扫描被挡住的微信）

# 验收①审计日志 11 项（要真 Key + 网络：3 次正常调用会花极少的钱，
#        另外故意发坏请求换 400 —— 那几次不花钱。它验的是"每次都留痕"）
python tests/dev_p1_verify.py

# 验收②跨进程写锁 + fsync 节流（离线）
python tests/dev_log_concurrency_verify.py

# 验收③遮挡校验回归（会临时造一个窗口盖住微信，几秒后销毁）
python tests/dev_occlusion_verify.py

# 验收④面板交互（需先起真面板，且开着调试日志）
JEV_HUD_DEBUG=1 python -m wechat_triage_hud.hud &
python tests/dev_p0_verify.py

# 验收⑤问题集四项指标（真实调用，约 $0.001 / 6 次）
python tests/eval_qset.py

# 验收⑥按钮/菜单/小球/单条消息判断（默认无头：屏幕上零出现）
python tests/dev_hud_buttons_verify.py
#   想亲眼看它弹窗（面板、菜单都真实显示）：
HUD_SELFCHECK_VISIBLE=1 python tests/dev_hud_buttons_verify.py
```

> 其中**真离线**的两套（`dev_log_concurrency_verify.py`、`dev_hud_buttons_verify.py`）随时可跑，
> 不联网、不花钱；其余要真微信或真 Key，只能在真机上手动跑（本项目**没有 CI**，全在本机验）。

## 文件

| 文件 | 是什么 | 需要微信/网络吗 |
| :--- | :--- | :--- |
| `dev_hud_buttons_verify.py` | 面板按钮 / 右键菜单 / 小球形态 / 点消息单判（151 项，默认无头；可视模式 152 项） | 不要（`wc.grab` 被换成抛错，审计写临时目录） |
| `dev_p0_verify.py` | 面板交互：点击送达、📌 固定与跟随、滚动、右键「退出」后进程归零 | 要真面板 + 可见微信 |
| `dev_p1_verify.py` | 审计日志"全都记"：重试各成一条、无 Key 明文、截断显式标记 | **要** Key 与网络（3 次真实调用，花费极小）|
| `dev_log_concurrency_verify.py` | 跨进程写锁不交织、fsync 按条数节流 | 不要（纯离线）|
| `dev_occlusion_verify.py` | 遮挡校验的阳性对照 / `ignore` / 回基线 / 边界 | 要可见微信（会临时造窗） |
| `dev_hud_survival.py` | 面板在微信被遮挡时不自己退出（排除"遮挡自退"误报） | 要（真机） |
| `dev_hud_cleanup.py` | 一次清掉所有面板实例（"只 kill 第一个"曾留下多个） | 不要 |
| `dev_layout_extract.py` | 版面与逐气泡提取（合成帧对照真值，不碰真实截图） | 不要 |
| `eval_qset.py` | 问题集四项指标：逃逸率 / 防编造 / 风险区分度 / 门控 | 要 Key（真实调用） |
| `oneoff/` | 一次性真机探针（第 2、3 轮真机复核用的临时脚本），留档不维护 | 多半要真机 |

## 写验收脚本的纪律（踩过的坑，别重犯）

- **别弹到用户屏幕上**：默认 `QT_QPA_PLATFORM=offscreen`；真要看时用 `HUD_SELFCHECK_VISIBLE=1`。
  真机复核抓到过"自检把右键菜单一次次弹在用户屏幕正中"。
- **菜单别用真 `exec()`**：`hud._build_context_menu()` 可以只建不弹（测试读项、`trigger()` 项）；
  offscreen 下 `QMenu.exec()` 的嵌套循环**永不返回**。
- **要看 `exec()` 里发生的事，必须用 QTimer**：`while processEvents()` 轮询会被嵌套循环卡住，看不到菜单。
- **`bar.r_body` 是 QLayout 不是 QWidget**：`findChildren()` 在它上面永远是空的；
  "键+值"还套在行容器里，要递归下钻。
- **`processEvents()` 不派发 DeferredDelete**：`deleteLater()` 的旧控件还会画在原地，截图会拍到重影。
