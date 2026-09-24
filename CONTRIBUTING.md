# 贡献指南

这个项目的核心纪律只有一条：**「能跑」不等于「已验证」**。任何结论（「已修复」「已支持」「更快了」）
都必须能指向一条**可复现的验证**或一份**真实日志**；没验证的就照实写「未验证」。

这条纪律是几次翻车换来的：自遮挡把面板自己的字读成会话名、缓存键不含身份导致「填了不生效」——
它们都不是单元测试能抓的，只有对着真窗口、真日志的验收能抓。

参与本项目即表示同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)；提问与反馈渠道见
[README.md](README.md) 的「支持与社区」。

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

- **外部依赖缺失必须"说出来"**：Key 没配 / 线程起不来这类"注定不会成功"的状态，
  必须在启动时或看门狗里写到状态行/页脚 —— 否则面板看着像在忙（用户就是这样报"怎么卡住了"）。
- **打包后必须跑 `wechat-triage-hud.exe --selftest-ocr`**：只验"exe 能启动"不够 —— 我们就是
  这样漏掉了"rapidocr 子模块没被打进包、OCR 一用就炸"（那两次冒烟测试时微信正好被遮挡，
  扫描在 OCR 之前就被拦下），最后是用户先撞到的。
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

- `README.md` —— 门面：安装、使用、配置、项目状态
- `CHANGELOG.md` —— 面向使用者的变更（Keep a Changelog 格式）
- `tests/README.md` —— 验收脚本清单、跑法与前置条件
- `SECURITY.md` —— 数据流、密钥与敏感文件的处理约定

## 提交信息

用 `类型(范围): 说明` 的形式（`feat` / `fix` / `docs` / `chore` / `test`），
说明里写清**用户看得见的变化**；有实测数字的（成本、耗时、置信度）就写进去。
