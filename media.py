"""素材文件夹：每个商品一个文件夹，机主把购买记录截图、实物视频放进去，买家要的时候机器人自动发。

文件夹放在项目的「素材」下，名字以商品 id 开头，例如  素材\1089400191282_周边小卡\
机器人第一次遇到某个商品会自动建好这个文件夹，机主只管往里放文件。
"""
import os
import re
import struct

from loguru import logger

BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_ROOT = os.path.join(BASE, '素材')
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v'}

# 买家说到这些，就是想要购买记录 / 实物素材
WANT_WORDS = ['购买记录', '购买截图', '采购记录', '订单截图', '购买凭证', '凭证', '购买证明', '进货记录', '进货截图', '官网截图',
              '对光', '实拍', '实物视频', '实物图', '拆封视频', '开箱视频', '发货视频', '验货视频', '视频', '实物']


def wants_media(text):
    return any(w in text for w in WANT_WORDS)


def _safe(name):
    return re.sub(r'[\\/:*?"<>|\s]+', '', name)[:12]


def item_folder(item_id):
    """找这个商品的素材文件夹（名字以商品 id 开头），没有返回 None"""
    if not os.path.isdir(MEDIA_ROOT):
        return None
    for d in os.listdir(MEDIA_ROOT):
        p = os.path.join(MEDIA_ROOT, d)
        if os.path.isdir(p) and re.match(rf'^{re.escape(str(item_id))}(?!\d)', d):
            return p
    return None


def ensure_item_folder(item_id, title=''):
    """没有就建一个：素材\\<商品id>_<标题前几个字>"""
    p = item_folder(item_id)
    if p:
        return p
    p = os.path.join(MEDIA_ROOT, f"{item_id}_{_safe(title)}" if title else str(item_id))
    os.makedirs(p, exist_ok=True)
    logger.info(f"已新建素材文件夹: {p}")
    return p


def list_media(folder):
    """文件夹里的图片和视频，按文件名排序"""
    imgs, vids = [], []
    for n in sorted(os.listdir(folder)):
        ext = os.path.splitext(n)[1].lower()
        full = os.path.join(folder, n)
        if not os.path.isfile(full):
            continue
        if ext in IMG_EXT:
            imgs.append(full)
        elif ext in VIDEO_EXT:
            vids.append(full)
    return imgs, vids


def image_size(path):
    """图片宽高（Pillow）"""
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def mp4_info(path):
    """从 mp4 / mov 的头里读 (宽, 高, 时长毫秒)，读不出来的给 0"""
    width = height = duration = 0
    with open(path, 'rb') as f:
        data = f.read(min(os.path.getsize(path), 64 * 1024 * 1024))

    def boxes(buf, start, end):
        i = start
        while i + 8 <= end:
            size, typ = struct.unpack('>I4s', buf[i:i + 8])
            hdr = 8
            if size == 1:
                size = struct.unpack('>Q', buf[i + 8:i + 16])[0]
                hdr = 16
            if size == 0:
                size = end - i
            if size < hdr:
                return
            yield typ, i + hdr, i + size
            i += size

    for typ, s, e in boxes(data, 0, len(data)):
        if typ != b'moov':
            continue
        for t2, s2, e2 in boxes(data, s, e):
            if t2 == b'mvhd':
                ver = data[s2]
                if ver == 1:
                    ts, dur = struct.unpack('>IQ', data[s2 + 20:s2 + 32])
                else:
                    ts, dur = struct.unpack('>II', data[s2 + 12:s2 + 20])
                duration = int(dur * 1000 / ts) if ts else 0
            elif t2 == b'trak':
                for t3, s3, e3 in boxes(data, s2, e2):
                    if t3 == b'tkhd':
                        ver = data[s3]
                        off = s3 + (88 if ver == 1 else 76)
                        w, h = struct.unpack('>II', data[off:off + 8])
                        if w >> 16 and h >> 16 and not width:
                            width, height = w >> 16, h >> 16
    return width, height, duration
