"""新建商品素材文件夹：粘贴商品链接，自动在「素材」下建好以商品 id 开头的文件夹。"""
import os
import re
import sys

import media

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

print("粘贴闲鱼商品链接（一次一个，直接回车结束）：")
while True:
    link = input("链接：").strip()
    if not link:
        break
    m = re.search(r'(?:[?&]id=|itemId=)(\d{8,})', link) or re.fullmatch(r'(\d{8,})', link)
    if not m:
        print("  没认出商品 id，请粘贴完整的商品链接")
        continue
    path = media.ensure_item_folder(m.group(1))
    print(f"  已准备好：{path}\n  把这个商品的购买记录截图、实物视频放进去就行")
os.startfile(media.MEDIA_ROOT) if os.name == 'nt' and os.path.isdir(media.MEDIA_ROOT) else None
