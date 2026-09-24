# -*- coding: utf-8 -*-
"""参考实现（原型，非产品代码）：按「气泡 = 消息 / 头像列 + 昵称条 = 说话人」从微信窗口提取聊天记录。

由 2026-09-23 的多轮真机验证得出，规则与实测数据见本机自用的版面实测记录（不入库）。
注意：这是验证用的一次性脚本，会抓当前微信窗口（需可见且未被遮挡）。
  用法：PYTHONPATH=. python tests/dev_bubble_extract_demo.py
  产出：out/extract_demo2.png（标注图）、out/chat_extract.json（结构化记录）
"""
# -*- coding: utf-8 -*-
import ctypes.wintypes as wt, json, re, numpy as np, cv2
from PIL import Image, ImageDraw, ImageFont
from wechat_triage_hud import wechat_window as ww, wechat_capture as wc
from rapidocr_onnxruntime import RapidOCR

w = ww.find_main()
occ, why = ww.occlusion(w.hwnd, w.rect, ignore=set())
if occ or w.minimized or not w.visible: print(f"✗ 抓不到：{why}"); raise SystemExit
img = wc.grab(w.rect); H, W = img.shape[:2]
v,c = np.unique(img[::6, W//2::6].reshape(-1,3), axis=0, return_counts=True); bg = v[c.argmax()]
isbg = (np.abs(img.astype(int)-bg).sum(-1) <= 12)
pane_left = int(np.where(isbg[int(H*0.25):int(H*0.7)].mean(0) > 0.3)[0][0])
rf = 1 - isbg[:, pane_left:].mean(1); fr = [y for y in range(H) if rf[y] > 0.90]
msg_top = next(y for y in fr if y > H*0.04); msg_bot = next(y for y in fr if y > H*0.6)
print(f"帧 {W}x{H}  msg_top={msg_top} msg_bot={msg_bot} pane_left={pane_left}")

e = RapidOCR(use_angle_cls=False)
e.text_detector.preprocess_op[0].limit_type = "max"
e.text_detector.preprocess_op[0].limit_side_len = 4000

# 掩码放宽到整段（不再切在 msg_bot），靠后面的可见性规则筛
m = np.zeros_like(isbg); m[msg_top:, pane_left:] = True
mask = ((~isbg) & m).astype(np.uint8)
n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
av, bub, img_msg, dropped = [], [], [], []
for i in range(1, n):
    x,y,bw,bh,area = st[i]
    if area < 300: continue
    if x+bw > W-18 and bh > 60: continue                       # 滚动条
    comp = img[y:y+bh, x:x+bw].reshape(-1,3)
    u,cc = np.unique(comp, axis=0, return_counts=True)
    flat = cc.max()/len(comp); fill = tuple(int(t) for t in u[cc.argmax()])
    vis = min(y+bh, msg_bot) - y                            # 在消息区内的可见高度
    if 20<=bw<=70 and 20<=bh<=70 and 0.6<=bw/(bh or 1)<=1.6 and flat < 0.5:
        av.append((int(x),int(y),int(bw),int(bh)))
    elif area > 2500 and bw >= 60 and flat < 0.55 and area/(bw*bh) >= 0.4:
        (img_msg if vis >= 20 else dropped).append((int(x),int(y),int(bw),int(bh),fill,vis))
    elif bh >= 24 and bw >= 28 and flat > 0.35 and area/(bw*bh) >= 0.4:
        (bub if vis >= 20 else dropped).append((int(x),int(y),int(bw),int(bh),fill,vis))
bub.sort(key=lambda b:b[1])
print(f"气泡 {len(bub)} 个、图片块 {len(img_msg)} 个、被丢掉的边界残块 {len(dropped)} 个")
for dd in dropped: print(f"   丢掉: y={dd[1]} h={dd[3]} 可见高 {dd[5]}px")

def rd(x,y,bw,bh,mins=0.3,scale=1.0):
    crop = img[max(0,int(y)):int(y)+int(bh), max(0,int(x)):int(x)+int(bw)]
    if crop.size==0 or crop.shape[0]<6 or crop.shape[1]<6: return ""
    if scale != 1.0: crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    res,_ = e(crop)
    return " ".join(t for _,t in sorted(((min(p[1] for p in b), t) for b,t,s in (res or []) if float(s)>=mins))).strip()
def name_of(y, x0=None):
    t = rd(x0 or (pane_left+50), y-32, 170, 28, 0.35, scale=2.0)
    if t and len(t)<=12 and not re.search(r"[:：]", t) and not re.fullmatch(r"[\d\s:：.月日年星期二三四五六日/-]+", t) and not any(ch in t for ch in "，。？！!?"): return t
    return None

recs, prev, claimed = [], None, set()
for (x,y,bw,bh,fill,vis) in bub:
    mine = (x+bw/2) > (pane_left + (W-7))/2
    if mine: nm = "我"
    else:
        f = name_of(y, pane_left+50); nm = f if f else (prev or "?"); prev = f or prev
    recs.append({"speaker":nm,"text":rd(x+5,y+4,bw-10,bh-8),"y":y,"x":x,"w":bw,"h":bh,
                 "side":"me" if mine else "other","trunc": (y+bh) > msg_bot+2})
    for (ax,ay,aw,ah) in av:
        if -8 <= ay-y <= 130 and abs(ax - ((W-70) if mine else (pane_left+20))) < 14: claimed.add(ay)
for (x,y,bw,bh,fill,vis) in img_msg:                            # 图片消息：正向证据 + 归属到头像
    owner = min(av, key=lambda a: abs(a[1]+a[3]-y)) if av else None
    mine = x + bw/2 > (pane_left + (W-7))/2
    nm = "我" if mine else None
    if not mine and owner:
        t = rd(owner[0]+44, owner[1]+2, 170, 26, 0.35, scale=2.0)   # 图片消息：昵称与头像同一行
        if t and len(t)<=12 and not re.search(r"[:：]", t) and not any(ch in t for ch in "，。？！!?"):
            nm = t; prev = t
        else:
            nm = "?"                                                     # 读不到就不猜
    if owner: claimed.add(owner[1])
    recs.append({"speaker": nm or "?","text":"[图片/表情]","y":y,"x":x,"w":bw,"h":bh,"side":"me" if mine else "other","trunc":False})
recs.sort(key=lambda r:r["y"])
print(f"\n=== 识别结果（{len(recs)} 条）===")
for i,r in enumerate(recs,1):
    print(f"{i:2d}. 【{r['speaker']}】{r['text']}{'  ← 底部被截断，文字可能不全' if r['trunc'] else ''}")
json.dump(recs, open("out/chat_extract.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)

pil = Image.fromarray(img); d = ImageDraw.Draw(pil, "RGBA")
F  = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 13)
Fb = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
for (x,y,bw,bh,fill,vis) in bub: d.rectangle([x,y,x+bw,min(y+bh,msg_bot)], outline=(225,45,45), width=2)
for (x,y,bw,bh,fill,vis) in img_msg: d.rectangle([x,y,x+bw,bh+y], outline=(150,60,200), width=3)
for d_ in dropped:
    if min(d_[1]+d_[3], msg_bot) > d_[1]: d.rectangle([d_[0],d_[1],d_[0]+d_[2],min(d_[1]+d_[3],msg_bot)], outline=(130,130,130), width=2)
for (x,y,bw,bh) in av: d.rectangle([x,y,x+bw,y+bh], outline=(20,155,60), width=3)
for i,r in enumerate(recs,1):
    lbl = f"{i}.【{r['speaker']}】{r['text'][:26]}"
    px = r["x"]+r["w"]+14 if r["side"]=="other" else max(pane_left+4, r["x"]-14-d.textlength(lbl,font=F))
    py = max(msg_top, r["y"]-2)
    if px + d.textlength(lbl,font=F) > W-10: px = max(pane_left+4, W-10-d.textlength(lbl,font=F))
    b = d.textbbox((px,py), lbl, font=F)
    d.rectangle([b[0]-3,b[1]-2,b[2]+3,b[3]+2], fill=(255,255,255,238), outline=(225,45,45), width=1)
    d.text((px,py), lbl, font=F, fill=(200,30,30))
d.text((pane_left+8, msg_bot+26), "红=气泡  紫=图片消息  灰=边界残块(已丢弃)  绿=头像", font=Fb, fill=(20,80,200))
pil.save("out/extract_demo2.png"); print("\n已保存 out/extract_demo2.png")
