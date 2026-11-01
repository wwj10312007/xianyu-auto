import json
import time
import os
import re
import sys

import requests
from loguru import logger
from utils.xianyu_utils import generate_sign


class XianyuApis:
    def __init__(self, env_path=None, account=None):
        # 每个账号一份 .env（里面有它自己的 COOKIES_STR），换 cookie 就是改这个文件
        self.env_path = env_path or os.path.join(os.getcwd(), '.env')
        self.account = account or '主号'
        self.url = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/'
        self.session = requests.Session()
        self.session.headers.update({
            'accept': 'application/json',
            'accept-language': 'zh-CN,zh;q=0.9',
            'cache-control': 'no-cache',
            'origin': 'https://www.goofish.com',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.goofish.com/',
            'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        })
        
    def clear_duplicate_cookies(self):
        """清理重复的cookies"""
        # 创建一个新的CookieJar
        new_jar = requests.cookies.RequestsCookieJar()
        
        # 记录已经添加过的cookie名称
        added_cookies = set()
        
        # 按照cookies列表的逆序遍历（最新的通常在后面）
        cookie_list = list(self.session.cookies)
        cookie_list.reverse()
        
        for cookie in cookie_list:
            # 如果这个cookie名称还没有添加过，就添加到新jar中
            if cookie.name not in added_cookies:
                new_jar.set_cookie(cookie)
                added_cookies.add(cookie.name)
                
        # 替换session的cookies
        self.session.cookies = new_jar
        
        # 更新完cookies后，更新.env文件
        self.update_env_cookies()
        
    def update_env_cookies(self):
        """更新.env文件中的COOKIES_STR"""
        try:
            # 获取当前cookies的字符串形式
            cookie_str = '; '.join([f"{cookie.name}={cookie.value}" for cookie in self.session.cookies])
            
            # 读取.env文件
            env_path = self.env_path
            if not os.path.exists(env_path):
                logger.warning(".env文件不存在，无法更新COOKIES_STR")
                return
                
            with open(env_path, 'r', encoding='utf-8') as f:
                env_content = f.read()
                
            # 使用正则表达式替换COOKIES_STR的值
            if 'COOKIES_STR=' in env_content:
                new_env_content = re.sub(
                    r'COOKIES_STR=.*', 
                    f'COOKIES_STR={cookie_str}',
                    env_content
                )
                
                # 写回.env文件
                with open(env_path, 'w', encoding='utf-8') as f:
                    f.write(new_env_content)
                    
                logger.debug("已更新.env文件中的COOKIES_STR")
            else:
                logger.warning(".env文件中未找到COOKIES_STR配置项")
        except Exception as e:
            logger.warning(f"更新.env文件失败: {str(e)}")
        
    def ck(self, name, default=''):
        """按名字取 cookie；服务端下发的新值和原来的同名 cookie 并存时（域不同）取最后一个，不抛 CookieConflictError"""
        val = default
        for c in self.session.cookies:
            if c.name == name:
                val = c.value
        return val

    def cookie_header(self):
        """当前 session 里的 cookie 拼成请求头字符串"""
        return '; '.join([f"{c.name}={c.value}" for c in self.session.cookies])

    def _read_env_cookie(self):
        """读本账号 .env 里的 COOKIES_STR，没有返回空串"""
        if not os.path.exists(self.env_path):
            return ''
        with open(self.env_path, 'r', encoding='utf-8') as f:
            for line in f.read().splitlines():
                if line.startswith('COOKIES_STR='):
                    return line[len('COOKIES_STR='):].strip()
        return ''

    def apply_cookie_str(self, cookie_str):
        """用一串新 cookie 整体替换 session 里的 cookie"""
        from utils.xianyu_utils import trans_cookies
        self.session.cookies.clear()
        self.session.cookies.update(trans_cookies(cookie_str))

    def wait_new_cookie(self, reason):
        """cookie 失效：通知机主，然后等他换上新 cookie（写进本账号 .env，不用重启程序）"""
        import notifier
        old = self._read_env_cookie()
        notifier.alert(
            f"cookie_dead:{self.account}",
            f"闲鱼自动回复：{self.account} 掉线了",
            f"{reason}\n这个号暂时不会自动回复。请双击「换登录.bat」重新扫码登录，换好后程序自动恢复，不用重启。",
        )
        logger.error(f"[{self.account}] {reason}，等待新的 cookie（双击「换登录.bat」扫码重新登录）")
        while True:
            time.sleep(15)
            new = self._read_env_cookie()
            if new and new != old:
                self.apply_cookie_str(new)
                logger.success(f"[{self.account}] 检测到新的 cookie，继续运行")
                return

    def hasLogin(self, retry_count=0):
        """调用hasLogin.do接口进行登录状态检查"""
        if retry_count >= 2:
            logger.error("Login检查失败，重试次数过多")
            return False
            
        try:
            url = 'https://passport.goofish.com/newlogin/hasLogin.do'
            params = {
                'appName': 'xianyu',
                'fromSite': '77'
            }
            data = {
                'hid': self.ck('unb', ''),
                'ltl': 'true',
                'appName': 'xianyu',
                'appEntrance': 'web',
                '_csrf_token': self.ck('XSRF-TOKEN', ''),
                'umidToken': '',
                'hsiz': self.ck('cookie2', ''),
                'bizParams': 'taobaoBizLoginFrom=web',
                'mainPage': 'false',
                'isMobile': 'false',
                'lang': 'zh_CN',
                'returnUrl': '',
                'fromSite': '77',
                'isIframe': 'true',
                'documentReferer': 'https://www.goofish.com/',
                'defaultView': 'hasLogin',
                'umidTag': 'SERVER',
                'deviceId': self.ck('cna', '')
            }
            
            response = self.session.post(url, params=params, data=data)
            res_json = response.json()
            
            if res_json.get('content', {}).get('success'):
                logger.debug("Login成功")
                # 清理和更新cookies
                self.clear_duplicate_cookies()
                return True
            else:
                logger.warning(f"Login失败: {res_json}")
                time.sleep(0.5)
                return self.hasLogin(retry_count + 1)
                
        except Exception as e:
            logger.error(f"Login请求异常: {str(e)}")
            time.sleep(0.5)
            return self.hasLogin(retry_count + 1)

    def get_token(self, device_id, retry_count=0, wait=True, relogin=0):
        """wait=False 时 cookie 失效直接抛 RuntimeError（登录工具验证用），不进入等待"""
        if retry_count >= 2:  # 最多重试3次
            logger.warning("获取token失败，尝试重新登陆")
            # 尝试通过hasLogin重新登录；hasLogin 报成功但 token 还是拿不到（会话真失效）连着 3 轮就按掉线处理，
            # 不再死循环刷登录接口
            if relogin < 3 and self.hasLogin():
                logger.info("重新登录成功，重新尝试获取token")
                return self.get_token(device_id, 0, wait, relogin + 1)  # 重置重试次数
            else:
                logger.error("重新登录失败，Cookie已失效")
                if not wait:
                    raise RuntimeError("Cookie 已失效")
                self.wait_new_cookie("Cookie 已失效（登录状态过期）")
                return self.get_token(device_id, 0, wait)

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idlemessage.pc.login.token',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
            "spm_pre": "a21ybx.item.want.1.14ad3da6ALVq3n",
            "log_id": "14ad3da6ALVq3n"
        }
        data_val = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + device_id + '"}'
        data = {
            'data': data_val,
        }
        headers = {
            "Host": "h5api.m.goofish.com",
            "sec-ch-ua-platform": "\"Windows\"",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "accept": "application/json",
            "sec-ch-ua": "\"Chromium\";v=\"146\", \"Not-A.Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
            "content-type": "application/x-www-form-urlencoded",
            "sec-ch-ua-mobile": "?0",
            "origin": "https://www.goofish.com",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": "https://www.goofish.com/",
            "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=1, i"
        }
        # 简单获取token，信任cookies已清理干净
        token = self.ck('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post('https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/', headers=headers, params=params, data=data)
            res_json = response.json()
            
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    # 检测风控/限流错误
                    error_msg = str(ret_value)
                    if 'RGV587_ERROR' in error_msg or '被挤爆啦' in error_msg:
                        logger.error(f"❌ 触发风控: {ret_value}")
                        if not wait:
                            raise RuntimeError(f"闲鱼要求验证（风控）：{ret_value}")
                        self.wait_new_cookie(f"闲鱼要求验证（风控）：{ret_value}")
                        return self.get_token(device_id, 0, wait)

                    logger.warning(f"Token API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")  # 降级为DEBUG并简化
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_token(device_id, retry_count + 1, wait, relogin)
                else:
                    logger.info("Token获取成功")
                    return res_json
            else:
                logger.error(f"Token API返回格式异常: {res_json}")
                return self.get_token(device_id, retry_count + 1, wait, relogin)
                
        except Exception as e:
            logger.error(f"Token API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_token(device_id, retry_count + 1, wait, relogin)

    def get_item_info(self, item_id, retry_count=0):
        """获取商品信息，自动处理token失效的情况"""
        if retry_count >= 3:  # 最多重试3次
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}
            
        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.pc.detail',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }
        
        data_val = '{"itemId":"' + item_id + '"}'
        data = {
            'data': data_val,
        }
        
        # 简单获取token，信任cookies已清理干净
        token = self.ck('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/', 
                params=params, 
                data=data
            )
            
            res_json = response.json()
            # 检查返回状态
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    logger.warning(f"商品信息API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_item_info(item_id, retry_count + 1)
                else:
                    logger.debug(f"商品信息获取成功: {item_id}")
                    return res_json
            else:
                logger.error(f"商品信息API返回格式异常: {res_json}")
                return self.get_item_info(item_id, retry_count + 1)
                
        except Exception as e:
            logger.error(f"商品信息API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_item_info(item_id, retry_count + 1)

    # ---------- 通用 mtop 请求（订单、改价、文件凭据都走它） ----------
    def mtop(self, api, data, version='1.0', retry=2):
        """调闲鱼网页版的 mtop 接口，返回 data 字段；失败抛 RuntimeError（带闲鱼原话）。"""
        data_val = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        last = None
        for _ in range(retry + 1):
            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': str(int(time.time()) * 1000),
                'sign': '',
                'v': version,
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': api,
                'sessionOption': 'AutoLoginOnly',
            }
            token = self.ck('_m_h5_tk', '').split('_')[0]
            params['sign'] = generate_sign(params['t'], token, data_val)
            response = self.session.post(
                f'https://h5api.m.goofish.com/h5/{api}/{version}/',
                params=params, data={'data': data_val}, timeout=20)
            res_json = response.json()
            ret = res_json.get('ret', [])
            if any('SUCCESS::调用成功' in r for r in ret):
                return res_json.get('data', {})
            last = ret
            if any('TOKEN_EMPTY' in r or 'TOKEN_EXOIRED' in r or 'ILLEGAL_ACCESS' in r for r in ret):
                # 服务端会在响应里下发新的 _m_h5_tk，session 已自动收下，换签名重试
                time.sleep(0.5)
                continue
            break
        raise RuntimeError(f"{api} 失败：{last}")

    # ---------- 我的在售商品（用来提前建素材文件夹） ----------
    def list_my_items(self, page_size=20, max_pages=30):
        """当前账号主页上的商品 [{item_id, title}]，翻页取完"""
        head = self.mtop('mtop.idle.web.user.page.head', {'self': True})
        uid = str((head.get('base') or head.get('baseInfo') or {}).get('kcUserId') or '').strip()
        if not uid:
            raise RuntimeError(f'无法解析当前账号 userId：{list(head.keys())}')
        out, seen = [], set()
        for page in range(1, max_pages + 1):
            try:
                data = self.mtop('mtop.idle.web.xyh.item.list', {'userId': uid, 'pageNumber': page, 'pageSize': page_size})
            except RuntimeError as e:
                if page == 1:
                    raise
                # 闲鱼限制能翻的页数：翻到头就用已经拿到的，下一轮再来
                logger.warning(f"[{self.account}] 商品列表翻到第 {page} 页被限制，已取 {len(out)} 个：{e}")
                break
            cards = data.get('cardList') or data.get('itemList') or []
            new = 0
            for c in cards:
                cd = (c or {}).get('cardData') or {}
                iid = str(cd.get('id') or (cd.get('detailParams') or {}).get('itemId') or '')
                if iid and iid not in seen:
                    seen.add(iid)
                    new += 1
                    out.append({'item_id': iid, 'title': cd.get('title') or (cd.get('detailParams') or {}).get('title') or ''})
            if not cards or not new or data.get('nextPage') is False:
                break
            time.sleep(1)
        return out

    # ---------- 订单 ----------
    @staticmethod
    def _order_id(item):
        raw = json.dumps(item.get('rightVO') or {}, ensure_ascii=False)
        m = re.search(r'"orderId":"(\d{10,})"', raw) or re.search(r'orderId=(\d{10,})', raw)
        if m:
            return m.group(1)
        oid = (item.get('commonData') or {}).get('orderId')
        return str(oid) if isinstance(oid, int) else ''

    def list_sold_orders(self, status='NOT_PAY', page=1, rows=20):
        """我是卖家的订单。status: ALL / NOT_PAY / NOT_SHIP / SHIPPED / TRADE_SUCCESS ..."""
        data = self.mtop('mtop.taobao.idle.trade.merchant.sold.get', {
            'pageNumber': page, 'rowsPerPage': rows, 'orderIds': '',
            'queryCode': status, 'orderSearchParam': '{}'})
        out = []
        for it in (data.get('module') or {}).get('items') or []:
            common = it.get('commonData') or {}
            out.append({
                'order_id': self._order_id(it),
                'item_id': str(common.get('itemId') or ''),
                'buyer_id': str((it.get('buyerInfoVO') or {}).get('buyerId') or ''),
                'buyer_nick': (it.get('buyerInfoVO') or {}).get('userNick') or '',
                'status': common.get('orderStatus') or '',
                'total_price': str((it.get('priceVO') or {}).get('totalPrice') or ''),
            })
        return out

    def find_order(self, buyer_id, item_id, statuses=('NOT_PAY',)):
        """找这个买家在这个商品上的订单（最新的一个）"""
        for st in statuses:
            for o in self.list_sold_orders(st):
                if o['buyer_id'] == str(buyer_id) and o['item_id'] == str(item_id) and o['order_id']:
                    return o
        return None

    def modify_order_price(self, order_id, price_yuan):
        """给拍下未付款的订单改价（只改商品价那一项，运费等原样带上）。返回 (改前, 改后)，元"""
        def render():
            d = self.mtop('mtop.taobao.idle.trade.order.modify.price.render', {'bizOrderId': order_id})
            lst = d.get('modifyPriceRenderList') or []
            if not lst:
                raise RuntimeError(f"订单 {order_id} 没有可改的价格项（已付款或已关闭）")
            return lst

        def cents(v):
            return int(round(float(v) * 100))

        lst = render()
        hit = next((x for x in lst if x.get('title') == '商品价格'), None) or next((x for x in lst if x.get('key') == 'modifyFee'), None)
        if not hit:
            raise RuntimeError(f"认不出商品价那一项：{lst}")
        key, before = hit['key'], hit['price']
        body = {'orderId': order_id}
        for x in lst:
            body[x['key']] = str(cents(price_yuan) if x['key'] == key else cents(x['price']))
        self.mtop('mtop.taobao.idle.trade.user.adjust.price', body)
        after = next(x for x in render() if x['key'] == key)['price']
        if cents(after) != cents(price_yuan):
            raise RuntimeError(f"改价后回读不一致：想改 {price_yuan}，实际 {after}")
        return before, after

    # ---------- 图片 / 视频上传 ----------
    def upload_media(self, path):
        """把本地图片传到闲鱼的聊天媒体服务，返回 (url, width, height)"""
        import mimetypes
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        with open(path, 'rb') as f:
            raw = f.read()
        resp = self.session.post(
            'https://stream-upload.goofish.com/api/upload.api',
            params={'floderId': '0', 'appkey': 'xy_chat', '_input_charset': 'utf-8'},
            files={'file': (name, raw, mime)}, timeout=120)
        res = resp.json()
        obj = res.get('object') or {}
        if not res.get('success') or not obj.get('url'):
            raise RuntimeError(f"上传 {name} 失败：{str(res)[:300]}")
        return obj['url'], int(obj.get('width') or 0), int(obj.get('height') or 0), res

    def upload_file_oss(self, session_id, my_id, path, name=None):
        """文件消息（App 里的「文件」）：传到闲鱼的 OSS 桶，返回消息里要的 file 字段。视频按文件发走这条。"""
        import base64, hashlib, hmac, mimetypes, urllib.parse, uuid
        from email.utils import formatdate
        name = name or os.path.basename(path)
        ext = os.path.splitext(name)[1].lstrip('.').lower()
        with open(path, 'rb') as f:
            raw = f.read()
        key = f"{session_id}/{my_id}/{time.time_ns() // 1000}-{uuid.uuid4()}" + (f".{ext}" if ext else "")
        tok = self.mtop('mtop.taobao.idlemessage.file.token.v1', {'sessionId': session_id, 'sessionType': 1, 'fileKey': key})
        bucket, host = 'xy-im-file', 'xy-im-file.oss-cn-hangzhou.aliyuncs.com'
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        date = formatdate(usegmt=True)
        to_sign = f"PUT\n\n{ctype}\n{date}\nx-oss-security-token:{tok['securityToken']}\n/{bucket}/{key}"
        sig = base64.b64encode(hmac.new(tok['accessKeySecret'].encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
        resp = requests.put(f"https://{host}/{urllib.parse.quote(key)}", data=raw, timeout=300, headers={
            'Content-Type': ctype, 'Date': date, 'x-oss-security-token': tok['securityToken'],
            'Authorization': f"OSS {tok['accessKeyId']}:{sig}"})
        if resp.status_code != 200:
            raise RuntimeError(f"文件上传 OSS 失败：{resp.status_code} {resp.text[:300]}")
        etag = (resp.headers.get('ETag') or '').strip('"').lower()
        if etag != hashlib.md5(raw).hexdigest():
            raise RuntimeError(f"文件上传后 ETag {etag} 和 MD5 对不上")
        return {'displayName': name, 'fileKey': key, 'fileSize': len(raw), 'fileType': ext}
