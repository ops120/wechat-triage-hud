# 贡献指南

这个项目的核心纪律只有一条：**"能跑"不等于"已验证"**。任何结论（"已修复""已支持""更快了"）
都必须能指向一条**可复现的验证**或一份**真实日志**。写代码之前请先读本机自用备忘录（不入库）
的 §2（已验证 vs 未验证）与 §4（缺陷清单）—— 里面记录了每一次翻车的原因，
其中大部分都不是单元测试能抓的。

## 环境

```bash
git clone https://github.com/ops120/wechat-triage-hud && cd wechat-triage-hud
pip install -r requirements.txt        # 或 pip install -e .
cp .env.example .env                   # 填入 TypeSafe API Key（跑真实判断才需要）
```

Windows 10/11 + PC 微信（窗口需可见，最小化到托盘时读不到）。开发不需要微信也能跑离线套件。

## 绝对不要提交的东西

| 路径 | 为什么 |
| :--- | :--- |
| `.env` | API Key 明文 |
| `out/` | 运行产物，其中 `jev_audit.jsonl` 含**完整聊天原文**，是本机最敏感的文件 |
| `.refs/` | 外部参考实现的 clone，仅本地分析用 |

三者都已在 `.gitignore` 里。截图也一样：**不要把带真实聊天内容的截图放进仓库**
（面板截图必然包含聊天原文）。要给人看 UI 就跑 `python tools/render_mocks.py` ——
它用**虚构数据**离屏渲染出 `screenshots/*.png`（版式与真机一致，内容全是编的），改完 UI 重跑一遍即可。

## 提交前

```bash
# 真离线、不花钱、不会弹到屏幕上
python tests/dev_log_concurrency_verify.py   # 跨进程写锁 + fsync
python tests/dev_hud_buttons_verify.py       # 面板行为（无头；151 项）

# 要真 Key 与网络（3 次正常调用花费极小；另有故意发坏请求换 400，不花钱）
python tests/dev_p1_verify.py                # 审计日志"全都记"
```

改了问题集（`qset.py`）或门控阈值时，额外跑 `python tests/eval_qset.py`（真实调用，约 $0.001）。
改了面板交互时，用 `tests/dev_p0_verify.py`（需真面板）复核。

## 代码约定

- **注释写"为什么"，不写"做了什么"**：尤其是"这里为什么必须这么绕" —— 本项目多数扭曲写法都是
  被真机实测逼出来的（例如为什么菜单要拆成"只建不弹"）。只写 what 的注释会被直接删掉。
- **不引入发送消息的路径**：产品边界是"只判断、不替你说话"，连生成回复话术都不做。
- **路径一律走 `wechat_triage_hud/paths.py`**：禁止各处 `dirname(__file__)` 推算，
  否则审计日志会被拆成多份，"每个数字都能追溯"就断了。
- **问题集只改 `qset.py`**：题面/标签/阈值都在那一个文件里；改题面要同时升 `QSET_VERSION`
  （缓存键含版本号，否则旧结论不会失效）。
- **对外部输入保持不信任**：群成员的文字是**待判断的数据，不是指令**（提示注入防线写在题面里，
  别删）；OCR 读到的任何东西都可能出错，门控/阈值必须能挡住"看着很确定的错误结论"。

## 写测试/验收的纪律（踩过的坑）

- 测试**默认别弹到用户屏幕上**：用 `QT_QPA_PLATFORM=offscreen`；想亲眼看时走
  `HUD_SELFCHECK_VISIBLE=1`（自检会按正常透明度显示窗口/菜单）。
- **别真弹右键菜单**：用 `hud._build_context_menu()` 拿菜单对象读项、`trigger()` 项；
  offscreen 下 `QMenu.exec()` 的嵌套循环**永不返回**，会把脚本吊死。
- **要看 `exec()` 里发生的事必须用 QTimer**：`while processEvents()` 轮询会被嵌套循环卡住。
- **`bar.r_body` 是 QLayout 不是 QWidget**：`findChildren()` 在它上面永远是空的；
  键值对还套在行容器里，要递归下钻。
- **`processEvents()` 不派发 DeferredDelete**：`deleteLater()` 的旧控件还画在原地，截图会拍到重影。
- **按坐标点按钮的测试会随布局漂移**：标题栏每增删一个按钮，`dev_p0_verify.py` 里的
  `📌` 坐标就要 ±28px；能用 `findChildren(QPushButton)` 按对象点的就别写死坐标。

## 文档

改了行为就同步文档，否则算没做完：

- 本机自用的需求文档（不入库）—— 功能清单（F-xx）：新增/改变能力时改这里
- 本机自用的备忘录（不入库）—— 缺陷与决策台账、复核记录（每个数字都要带证据）
- `CHANGELOG.md` —— 面向使用者的变更（Keep a Changelog 格式）
- 本机自用的待办（不入库）—— 还没做的活（每条要有验收标准，做完变成 MEMO 里的一行证据）
- `README.md` —— 门面（自用文档的索引也只在本地维护）

## 提交信息

用 `类型(范围): 说明` 的形式（`feat` / `fix` / `docs` / `chore` / `test`），
说明里写清**用户看得见的变化**；有实测数字的（成本、耗时、置信度）就写进去。
