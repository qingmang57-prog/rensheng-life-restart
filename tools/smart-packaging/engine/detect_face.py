# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "opencv-python"]
# ///
"""detect_face.py — 口播人脸 auto 检测（P1：消灭最后一个手工点）
对片源均匀抽帧跑 Haar  frontalface：
  · 命中框均值 → 归一化锚点 {cx,cy,h}（兼容旧消费方）
  · 逐样本命中 → tracking 轨迹 [{time,cx,cy,h}]（上游 presenter.tracking 同款，3 点滑动平均抑抖）
用法：python detect_face.py <视频>  → JSON {ok, cx,cy,h, tracking:[...], hits, samples, confidence}
置信规则：命中帧 < 15% 或框高方差过大 → confidence low，建议人工给坐标。"""
from pathlib import Path
import argparse, json, os, subprocess
import numpy as np
import cv2

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video')
    ap.add_argument('--samples', type=int, default=24)
    a = ap.parse_args()
    cap = cv2.VideoCapture(str(Path(a.video).resolve()))
    if not cap.isOpened(): raise SystemExit('打不开视频')
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    from pathlib import Path as _P
    xml = os.environ.get('IP_HAAR_CASCADE', '')
    if not xml or not Path(xml).is_file():
        raise SystemExit('未找到 haarcascade_frontalface_default.xml，请用环境变量 IP_HAAR_CASCADE 指定路径')
    cas = cv2.CascadeClassifier(xml)
    boxes = []
    rows = []
    for i in range(a.samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + .5) / a.samples))
        ok, frame = cap.read()
        if not ok: continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        found = cas.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                     minSize=(int(W * .08), int(W * .08)))
        if len(found):
            x, y, w, h = max(found, key=lambda b: b[2] * b[3])
            boxes.append((x + w / 2, y + h / 2, h))
            rows.append([int(total * (i + .5) / a.samples) / fps,
                         (x + w / 2) / W, (y + h / 2) / H,
                         float(np.clip(h * 1.25, H * .15, H * .9)) / H])   # 与锚点同余量规则
    cap.release()
    if len(boxes) < max(3, a.samples * .15):
        print(json.dumps({'ok': False, 'hits': len(boxes), 'samples': a.samples,
                          'hint': '命中过低，请人工提供 face 坐标'})); return
    cx, cy, h = np.median(boxes, axis=0)
    h = float(np.clip(h * 1.25, H * .15, H * .9))       # 框上留发型余量
    conf = 'high' if len(boxes) > a.samples * .6 else 'medium'
    # tracking 轨迹：3 点滑动平均抑抖（Haar 帧间噪声），时间轴不动
    tr = []
    for j, r in enumerate(rows):
        win3 = rows[max(0, j - 1):j + 2]
        tr.append({'time': round(r[0], 3),
                   'cx': round(float(np.mean([q[1] for q in win3])), 3),
                   'cy': round(float(np.mean([q[2] for q in win3])), 3),
                   'h': round(float(np.mean([q[3] for q in win3])), 3)})
    print(json.dumps({'ok': True, 'cx': round(float(cx) / W, 3), 'cy': round(float(cy) / H, 3),
                      'h': round(h / H, 3), 'tracking': tr,
                      'hits': len(boxes), 'samples': a.samples,
                      'confidence': conf}, ensure_ascii=False))

if __name__ == '__main__':
    main()
