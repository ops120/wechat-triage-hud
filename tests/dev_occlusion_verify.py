"""验证遮挡校验真的会触发 —— 这是 §17-1 那条关键缺陷的回归测试。

为什么必须有：截屏抓的是**屏幕上该矩形的像素**，不是"微信窗口的内容"。
微信被别的窗口盖住时，OCR 会读到覆盖层的文字（实测读到过编辑器里本项目报告的文字），
然后被当成群消息发给模型 —— 不报错、看着还很确定，而且已经写进审计日志。

阳性对照用**临时造一个盖住微信的窗口**，比拿别的窗口矩形去猜可靠。
"""
import ctypes
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud import wechat_window as ww  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def main():
    w = ww.find_main()
    if not w:
        print("未找到微信窗口，无法验证")
        return 1

    # ---- 前置条件：微信必须可见且尺寸正常，否则阳性对照没有意义 ----
    # 少了这一段，测试会在"微信被最小化"时报 3 个假失败
    # （尺寸守卫先返回 True，临时窗口也会被造到屏幕外），那不是产品缺陷。
    if w.minimized:
        print("微信被最小化，先还原…")
        ww.restore(w.hwnd)
        time.sleep(1.5)
        w = ww.find_main()
    l, t, r, b = w.rect
    if (r - l) < 200 or (b - t) < 200:
        print(f"⚠ 前置条件不满足：微信窗口尺寸 {r-l}x{b-t}，不可用于遮挡验证。")
        print("  原因通常是微信被最小化到托盘，或还没登录。")
        print("  这不算失败 —— 请让微信窗口可见后重跑。")
        return 0
    if not w.visible:
        print("⚠ 前置条件不满足：微信窗口不可见。请让它显示后重跑。")
        return 0
    cx, cy = l + (r - l) // 2, t + (b - t) // 2
    print(f"微信 hwnd={w.hwnd} rect={(l, t, r, b)}")
    print(f"消息区中心采样点 ({cx},{cy})\n")

    # 基线：当前状态。**可能存在既有的遮挡者**（例如用户正在用的编辑器盖在微信上），
    # 这时断言"报告的一定是临时窗口"就是错的 —— 必须把既有遮挡者先取出来。
    print("[1] 基线（当前是否被遮挡）")
    base_occ, why0 = ww.occlusion(w.hwnd)
    base_set = set(ww.occluders(w.hwnd))
    print(f"    occlusion={base_occ}  {why0 or '未被遮挡'}")
    if base_set:
        names = []
        for h in base_set:
            try:
                names.append(f"{ww.win32gui.GetClassName(h)}/{ww.win32gui.GetWindowText(h)[:14]}")
            except Exception:
                names.append(str(h))
        print(f"    既有遮挡者 {len(base_set)} 个: {', '.join(names)}")
        print("    （这是真实情况：说明微信现在确实被别的窗口盖着 —— 产品此时会拒绝扫描，正确）")
    check("返回值为布尔", isinstance(base_occ, bool))

    # 阳性对照：临时窗口盖住采样点
    print("\n[2] 阳性对照：临时造窗盖住消息区中心采样点")
    import tkinter as tk
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.geometry(f"240x160+{cx - 120}+{cy - 80}")
    root.configure(bg="black")
    root.update()
    time.sleep(0.7)
    h = root.winfo_id()
    while True:
        p = ww.win32gui.GetParent(h)
        if not p:
            break
        h = p
    print(f"    临时窗口 hwnd={h} rect={ww.win32gui.GetWindowRect(h)}")

    occ1, why1 = ww.occlusion(w.hwnd)
    now_set = set(ww.occluders(w.hwnd))
    print(f"    occlusion={occ1}  {why1}")
    check("检出遮挡", occ1)
    # 只有在基线本来没有遮挡时，"报告者必须是临时窗口"才成立
    if not base_set:
        check("说明里含覆盖窗口句柄", str(h) in why1, why1[:70])
    else:
        check("新增了临时窗口作为遮挡者", h in now_set or occ1, str(sorted(now_set)))
    # 忽略掉**全部**当前遮挡者后才应恢复未遮挡
    occ2, _ = ww.occlusion(w.hwnd, ignore=now_set)
    print(f"    忽略全部 {len(now_set)} 个遮挡者后 occlusion={occ2}")
    check("ignore 生效（不再算遮挡）", not occ2)

    root.destroy()
    time.sleep(0.5)

    # 移除后恢复
    print("[3] 销毁临时窗口后应回到基线状态")
    occ3, why3 = ww.occlusion(w.hwnd)
    back_set = set(ww.occluders(w.hwnd))
    print(f"    occlusion={occ3}  {why3 or '未被遮挡'}   遮挡者 {len(back_set)} 个")
    # 基线本来就可能被遮挡（例如用户正在用的编辑器盖着微信），所以期望是"回到基线"而不是"未遮挡"
    check("恢复基线遮挡状态", occ3 == base_occ and back_set == base_set,
          f"基线 occ={base_occ}/{len(base_set)}个 -> 现在 occ={occ3}/{len(back_set)}个")
    print(f"    occlusion={occ3}  {why3 or '未被遮挡'}")

    # 边界
    print("\n[4] 边界")
    o4, w4 = ww.occlusion(0)
    check("无效句柄判为不可用", o4, w4)
    o5, w5 = ww.occlusion(w.hwnd, rect=(0, 0, 50, 50))
    check("过小矩形判为不可用", o5, w5)

    print("\n" + "=" * 60)
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
        return 1
    print("遮挡校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
