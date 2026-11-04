import base64
import json
import asyncio
import time
import os
import websockets
from loguru import logger
from dotenv import load_dotenv, set_key
from XianyuApis import XianyuApis
import sys
import random
import re

import media
import notifier
from reply_rules import (
    ScriptBook, attach_seller_id, build_handoff_notice, classify_role,
    explain_seller_fields, explain_stuck, guard_ordered_reply, looks_uncertain,
    should_skip_as_buyer, suggest_replies, summarize_chat,
)
from store import Store
from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 买家说到这些就直接转人工（不过大模型）
HANDOFF_WORDS = ['人工', '真人', '投诉', '举报', '退款', '退货', '维权', '平台介入', '差评']
STALL_REPLIES = ['稍等宝宝 我确认下', '宝宝稍等 我看下', '老师稍等 马上回你']
NEED_ORDER_REPLIES = ['宝宝拍下发购买记录', '拍下就发购买记录哦宝宝', '老师拍下发你购买记录']
NEED_ORDER_REPLIES_OTHER = ['宝宝拍下就发', '拍下马上发你宝宝', '老师拍下就发']
PRICE_DONE_REPLIES = ['改好了宝宝 可以付款了', '宝宝价格改好了 付款吧', '老师改好了 直接付就行']
ORDERED_SHIPPING_REPLY = '等货到了隔一两天发货'
NOT_ORDERED_SHIPPING_REPLY = '货到了直接转寄'

# 买家的收尾话（整句只有这些）：不用回，回了反而像机器人
CLOSING_RE = re.compile(
    r'^(ok|okay|好+的?|好哒|好滴|好嘞|好吧|好呢|嗯+|哦+|噢+|行|可以|可以的|收到|知道了|了解|明白|懂了|没问题|谢谢|谢啦|多谢|感谢|辛苦了|拜拜|再见|拍了|好的呢|老师|宝宝|宝|亲|哥|姐)+$',
    re.I)
CLAIM_DIR = os.path.join(BASE_DIR, 'accounts', '.claims')


def is_closing(text):
    t = re.sub(r'[\s\W_]+', '', text or '')
    return bool(t) and len(t) <= 12 and bool(CLOSING_RE.match(t))


def is_shipping_time_question(text):
    """判断买家是否在询问发货时间或发货时效。"""
    t = re.sub(r'[\s\W_]+', '', text or '')
    if not t:
        return False
    # 购买记录等素材问题走原来的素材处理流程，不要误判成发货问题。
    if re.search(r'购买记录|购买截图|订单截图|购买凭证|凭证|实物|视频|照片|图片|素材', t):
        return False
    shipping = r'(?:发货|发出|寄出|转寄|发快递|安排发)'
    time_words = r'(?:什么时候|啥时候|何时|几时|多久|几天|哪天|几号|时间|时效|日期)'
    return bool(
        re.search(rf'{time_words}.{{0,8}}{shipping}', t)
        or re.search(rf'{shipping}.{{0,8}}{time_words}', t)
        or re.search(rf'{time_words}.{{0,3}}发$', t)
    )


def claim_message(chat_id, sender_id, create_time, text):
    """认领一条买家消息，返回 True 才由这个程序回复。
    多个号可能同时收到同一条（关联账号的会话会同步给每个号），重连也会把最近的消息重放一遍；
    认领记在磁盘上，所有号的程序和重启前后共用，一条买家消息只会被一个程序回一次。"""
    import hashlib
    os.makedirs(CLAIM_DIR, exist_ok=True)
    h = hashlib.sha1(f'{chat_id}|{sender_id}|{create_time}|{text}'.encode('utf-8')).hexdigest()
    try:
        os.close(os.open(os.path.join(CLAIM_DIR, 'm_' + h), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    # 同一个人在同一会话 2 分钟内发一模一样的话，也只回一次（重放时间戳不一致的兜底）
    t = os.path.join(CLAIM_DIR, 't_' + hashlib.sha1(f'{chat_id}|{sender_id}|{text}'.encode('utf-8')).hexdigest())
    try:
        if os.path.exists(t) and time.time() - os.path.getmtime(t) < 120:
            return False
        with open(t, 'w') as f:
            f.write(str(time.time()))
    except OSError as e:
        logger.warning(f'认领文件写入失败：{e!r}')
    return True


def own_user_ids():
    """本机所有已登录账号的闲鱼用户 id（cookie 里的 unb）。自己几个号之间的消息不当买家处理，免得互相对聊。"""
    ids = set()
    root = os.path.join(BASE_DIR, 'accounts')
    if not os.path.isdir(root):
        return ids
    for name in os.listdir(root):
        env = os.path.join(root, name, '.env')
        if not os.path.isfile(env):
            continue
        with open(env, encoding='utf-8') as f:
            m = re.search(r'unb=(\d+)', f.read())
        if m:
            ids.add(m.group(1))
    return ids


def cleanup_claims(max_age=86400):
    if not os.path.isdir(CLAIM_DIR):
        return
    now = time.time()
    for n in os.listdir(CLAIM_DIR):
        p = os.path.join(CLAIM_DIR, n)
        try:
            if now - os.path.getmtime(p) > max_age:
                os.remove(p)
        except OSError as e:
            logger.warning(f'清理认领文件失败 {n}：{e!r}')


class XianyuLive:
    def __init__(self, cookies_str, account='主号', account_dir=None, bot=None):
        self.account = account
        self.account_dir = account_dir or BASE_DIR
        self.bot = bot
        self.xianyu = XianyuApis(env_path=os.path.join(self.account_dir, '.env'), account=account)
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.xianyu.session.cookies.update(self.cookies)  # 直接使用 session.cookies.update
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        db_path = os.path.join(self.account_dir, 'data', 'chat_history.db')
        self.context_manager = ChatContextManager(db_path=db_path)
        self.store = Store(db_path)
        # 机主回过的话，各账号共用这一本，下次同类问题优先用
        self.scripts = ScriptBook(os.path.join(BASE_DIR, 'data', 'learned_scripts.db'))
        self._my_items_cache = None
        self._my_items_at = 0
        self._item_fetch_fail = {}
        self.fail_streak = 0                     # 连续连接失败次数，到 5 次通知机主
        self.auto_price = os.getenv("AUTO_PRICE", "True").lower() == "true"
        self.min_price_ratio = float(os.getenv("AUTO_PRICE_MIN_RATIO", "0.7"))   # 成交价不能低于标价的这个比例
        self.media_require_order = os.getenv("MEDIA_REQUIRE_ORDER", "True").lower() == "true"  # 买家拍下后才发素材
        self._price_checked = {}
        self._reconciling = False
        
        # 心跳相关配置
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # 心跳间隔，默认15秒
        self.heartbeat_timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "5"))     # 心跳超时，默认5秒
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        
        # Token刷新相关配置
        self.token_refresh_interval = int(os.getenv("TOKEN_REFRESH_INTERVAL", "3600"))  # Token刷新间隔，默认1小时
        self.token_retry_interval = int(os.getenv("TOKEN_RETRY_INTERVAL", "300"))       # Token重试间隔，默认5分钟
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        
        # 人工接管相关配置
        self.manual_mode_conversations = set()  # 存储处于人工接管模式的会话ID
        self.manual_mode_timeout = int(os.getenv("MANUAL_MODE_TIMEOUT", "3600"))  # 人工接管超时时间，默认1小时
        self.manual_mode_timestamps = {}  # 记录进入人工模式的时间
        
        # 消息过期时间配置
        self.message_expire_time = int(os.getenv("MESSAGE_EXPIRE_TIME", "300000"))  # 消息过期时间，默认5分钟
        
        # 人工接管关键词，从环境变量读取
        self.toggle_keywords = os.getenv("TOGGLE_KEYWORDS", "。")
        
        # 模拟人工输入配置
        self.simulate_human_typing = os.getenv("SIMULATE_HUMAN_TYPING", "False").lower() == "true"

    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info("开始刷新token...")
            
            # 获取新token（如果Cookie失效，get_token会直接退出程序）
            token_result = await asyncio.to_thread(self.xianyu.get_token, self.device_id)
            if 'data' in token_result and 'accessToken' in token_result['data']:
                new_token = token_result['data']['accessToken']
                self.current_token = new_token
                self.last_token_refresh_time = time.time()
                # 服务端会下发新的 cookie；换过登录的话账号 id 也可能变，都跟上
                self.cookies_str = self.xianyu.cookie_header()
                unb = self.xianyu.ck('unb')
                if unb and unb != self.myid:
                    self.myid = unb
                    self.device_id = generate_device_id(unb)
                logger.info("Token刷新成功")
                return new_token
            else:
                logger.error(f"Token刷新失败: {token_result}")
                return None
                
        except Exception as e:
            logger.error(f"Token刷新异常: {str(e)}")
            return None

    async def token_refresh_loop(self):
        """Token刷新循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要刷新token
                if current_time - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token即将过期，准备刷新...")
                    
                    new_token = await self.refresh_token()
                    if new_token:
                        logger.info("Token刷新成功，准备重新建立连接...")
                        # 设置连接重启标志
                        self.connection_restart_flag = True
                        # 关闭当前WebSocket连接，触发重连
                        if self.ws:
                            await self.ws.close()
                        break
                    else:
                        logger.error("Token刷新失败，将在{}分钟后重试".format(self.token_retry_interval // 60))
                        await asyncio.sleep(self.token_retry_interval)  # 使用配置的重试间隔
                        continue
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Token刷新循环出错: {e}")
                await asyncio.sleep(60)

    async def send_msg(self, ws, cid, toid, text):
        text = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        text_base64 = str(base64.b64encode(json.dumps(text).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def init(self, ws):
        # 如果没有token或者token过期，获取新token
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info("获取初始token...")
            await self.refresh_token()
        
        if not self.current_token:
            logger.error("无法获取有效token，初始化失败")
            raise Exception("Token获取失败")
            
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 等待一段时间，确保连接注册完成
        await asyncio.sleep(1)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')

    def is_chat_message(self, message):
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict) 
                and "1" in message 
                and isinstance(message["1"], dict)  # 确保是字典类型
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)  # 确保是字典类型
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and "@goofish" in message["1"][0]["1"]
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False
    
    def is_bracket_system_message(self, message):
        """检查是否为带中括号的系统消息"""
        try:
            if not message or not isinstance(message, str):
                return False
            
            clean_message = message.strip()
            # 检查是否以 [ 开头，以 ] 结尾
            if clean_message.startswith('[') and clean_message.endswith(']'):
                logger.debug(f"检测到系统消息: {clean_message}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查系统消息失败: {e}")
            return False

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return message_stripped in self.toggle_keywords

    def is_manual_mode(self, chat_id):
        """检查特定会话是否处于人工接管模式"""
        if chat_id not in self.manual_mode_conversations:
            return False
        
        # 检查是否超时
        current_time = time.time()
        if chat_id in self.manual_mode_timestamps:
            if current_time - self.manual_mode_timestamps[chat_id] > self.manual_mode_timeout:
                # 超时，自动退出人工模式
                self.exit_manual_mode(chat_id)
                return False
        
        return True

    def enter_manual_mode(self, chat_id):
        """进入人工接管模式"""
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()

    def exit_manual_mode(self, chat_id):
        """退出人工接管模式"""
        self.manual_mode_conversations.discard(chat_id)
        if chat_id in self.manual_mode_timestamps:
            del self.manual_mode_timestamps[chat_id]

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            return "manual"
    
    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            return round(float(price) / 100, 2)
        except (ValueError, TypeError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0
    
    def build_item_description(self, item_info):
        """构建商品描述"""
        
        # 处理 SKU 列表
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        
        for sku in raw_sku_list:
            # 提取规格文本
            specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
            spec_text = " ".join(specs) if specs else "默认规格"
            
            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": sku.get('quantity', 0)
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]
        
        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            # 如果没有SKU价格，回退使用商品主价格
            main_price = round(float(item_info.get('soldPrice', 0)), 2)
            price_display = f"¥{main_price}"

        summary = {
            "title": item_info.get('title', ''),
            "desc": item_info.get('desc', ''),
            "price_range": price_display,
            "total_stock": item_info.get('quantity', 0),
            "sku_details": clean_skus
        }

        return json.dumps(summary, ensure_ascii=False)

    async def handle_message(self, message_data, websocket):
        """处理所有类型的消息"""
        try:

            try:
                message = message_data
                ack = {
                    "code": 200,
                    "headers": {
                        "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                        "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                    }
                }
                if 'app-key' in message["headers"]:
                    ack["headers"]["app-key"] = message["headers"]["app-key"]
                if 'ua' in message["headers"]:
                    ack["headers"]["ua"] = message["headers"]["ua"]
                if 'dt' in message["headers"]:
                    ack["headers"]["dt"] = message["headers"]["dt"]
                await websocket.send(json.dumps(ack))
            except Exception as e:
                pass

            # 如果不是同步包消息，直接返回
            if not self.is_sync_package(message_data):
                return

            # 一个同步包里可能同时带好几条消息，逐条处理（原版只看第一条，会漏消息）
            for idx in range(len(message_data["body"]["syncPushPackage"]["data"])):
                await self._handle_sync_item(message_data, websocket, idx)

        except Exception as e:
            logger.exception(f"处理消息时发生错误: {e!r}")

    async def _handle_sync_item(self, message_data, websocket, idx):
        """处理同步包里的第 idx 条消息"""
        try:
            # 获取并解密数据
            sync_data = message_data["body"]["syncPushPackage"]["data"][idx]
            
            # 检查是否有必要的字段
            if "data" not in sync_data:
                logger.debug("同步包中无data字段")
                return

            # 解密数据
            try:
                data = sync_data["data"]
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    data = json.loads(data)
                    # logger.info(f"无需解密 message: {data}")
                    return
                except Exception as e:
                    # logger.info(f'加密数据: {data}')
                    decrypted_data = decrypt(data)
                    message = json.loads(decrypted_data)
            except Exception as e:
                logger.error(f"消息解密失败: {e}")
                return

            try:
                # 判断是否为订单消息,需要自行编写付款后的逻辑
                if message['3']['redReminder'] == '等待买家付款':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'等待买家 {user_url} 付款')
                    self.spawn_reconcile(websocket)
                    return
                elif message['3']['redReminder'] == '交易关闭':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'买家 {user_url} 交易关闭')
                    return
                elif message['3']['redReminder'] == '等待卖家发货':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'交易成功 {user_url} 等待卖家发货')
                    self.spawn_reconcile(websocket)
                    return

            except:
                pass

            # 判断消息类型
            if self.is_typing_status(message):
                logger.debug("用户正在输入")
                return
            elif not self.is_chat_message(message):
                logger.debug("其他非聊天消息")
                logger.debug(f"原始消息: {message}")
                return

            # 处理聊天消息
            create_time = int(message["1"]["5"])
            send_user_name = message["1"]["10"]["reminderTitle"]
            send_user_id = message["1"]["10"]["senderUserId"]
            send_message = message["1"]["10"]["reminderContent"]
            
            # 时效性验证（过滤5分钟前消息）
            if (time.time() * 1000 - create_time) > self.message_expire_time:
                logger.debug("过期消息丢弃")
                return
                
            # 获取商品ID和会话ID
            url_info = message["1"]["10"]["reminderUrl"]
            item_id = url_info.split("itemId=")[1].split("&")[0] if "itemId=" in url_info else None
            chat_id = message["1"]["2"].split('@')[0]
            
            if not item_id:
                logger.warning("无法获取商品ID")
                return

            # 检查是否为卖家（自己）发送的控制命令
            if send_user_id == self.myid:
                logger.debug("检测到卖家消息，检查是否为控制命令")
                
                # 检查切换命令
                if self.check_toggle_keywords(send_message):
                    mode = self.toggle_manual_mode(chat_id)
                    if mode == "manual":
                        logger.info(f"🔴 已接管会话 {chat_id} (商品: {item_id})")
                    else:
                        logger.info(f"🟢 已恢复会话 {chat_id} 的自动回复 (商品: {item_id})")
                    return
                
                # 记录卖家人工回复，并把他刚说的话记成话术
                self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", send_message)
                logger.info(f"卖家人工回复 (会话: {chat_id}, 商品: {item_id}): {send_message}")
                self.remember_owner_reply(chat_id, item_id, send_message)
                return
            
            logger.info(f"用户: {send_user_name} (ID: {send_user_id}), 商品: {item_id}, 会话: {chat_id}, 消息: {send_message}")

            # 同机别的号发的话（关联账号的会话会同步过来）：不当买家，不回
            if send_user_id in own_user_ids():
                logger.info("发信人是本机另一个号，跳过")
                return

            # 先分清这个号是不是商品的卖家。真正的买家侧不认领这条消息，免得占掉卖家号的回复。
            item_info = self.load_item_info(item_id)
            role, why = await self.seller_role(item_id, item_info)
            if should_skip_as_buyer(role):
                logger.info(f"当前账号在会话商品 {item_id} 中是买家（依据：{why}），跳过自动回复")
                return
            if role == 'unknown':
                logger.warning(f"当前账号在会话商品 {item_id} 中卖家身份未确认（依据：{why}）。不能当成买家跳过，继续自动回复")
            else:
                logger.info(f"当前账号在会话商品 {item_id} 中是卖家（依据：{why}）")

            # 同一条消息只处理一次（多个号同时收到 / 重连重放）
            if not claim_message(chat_id, send_user_id, create_time, send_message):
                logger.info(f"重复消息，已由别处处理，跳过：{send_message[:30]}")
                return
            
            # 如果当前会话处于人工接管模式，不进行自动回复
            if self.is_manual_mode(chat_id):
                logger.info(f"🔴 会话 {chat_id} 处于人工接管模式，跳过自动回复")
                # 添加用户消息到上下文
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                return
            # 检查是否为带中括号的系统消息
            if self.is_bracket_system_message(send_message):
                logger.info(f"检测到系统消息：'{send_message}'，跳过自动回复")
                return
            if self.is_system_message(message):
                logger.debug("系统消息，跳过处理")
                return
            # 收尾话（ok / 好的 / 收到…）不回，只记进上下文
            if is_closing(send_message):
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                logger.info(f"[收尾话不回] {send_message}")
                return
            if not item_info:
                logger.warning(f"商品 {item_id} 详情没拿到，仍继续回复，避免本店咨询被丢掉")
                item_info = {}

            # 每次准备回复前查询该买家是否已经拍下这个商品，发货时间要按订单状态回答。
            buyer_has_order = None
            try:
                buyer_has_order = await self.has_order(send_user_id, item_id)
                logger.info(f"买家 {send_user_id} 是否已拍下商品 {item_id}: {buyer_has_order}")
            except Exception as e:
                # 订单查询失败时继续走普通回复，避免因为接口暂时异常漏掉买家消息。
                logger.warning(f"查询买家 {send_user_id} 订单失败，跳过订单专属回复：{e!r}")
                
            item_description=f"当前商品的信息如下：{self.build_item_description(item_info)}"

            # 第一次遇到这个商品就建好它的素材文件夹（机主把购买记录、实物视频放进去）
            media.ensure_item_folder(item_id, item_info.get('title', ''))

            # 买家要购买记录 / 实物素材：按商品文件夹自动发
            if media.wants_media(send_message):
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                await self.handle_media_request(websocket, chat_id, send_user_id, send_user_name, item_id, send_message, buyer_has_order, item_info)
                return

            # 发货时间按是否已有订单固定回答，避免模型把未拍下和已拍下的情况混在一起。
            if is_shipping_time_question(send_message) and buyer_has_order is not None:
                shipping_reply = ORDERED_SHIPPING_REPLY if buyer_has_order else NOT_ORDERED_SHIPPING_REPLY
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                await self.reply_text(websocket, chat_id, send_user_id, shipping_reply, item_id)
                return

            # 机主以前答过同类问题：直接用他的原话，不再让模型现编
            learned = self.scripts.find(send_message, item_id)
            if learned:
                learned_reply, dropped = guard_ordered_reply(learned['reply'], buyer_has_order)
                if dropped:
                    logger.info(f"买家已拍下，话术里的催拍已去掉：{dropped}")
                logger.info(f"命中机主话术（以前的问法「{learned['question']}」）：{learned_reply}")
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                await self.reply_text(websocket, chat_id, send_user_id, learned_reply, item_id)
                return

            # 买家点名要人工 / 退款投诉：直接转给机主
            if any(w in send_message for w in HANDOFF_WORDS):
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                await self.handoff(
                    websocket, chat_id, send_user_id, send_user_name, item_id, send_message,
                    "买家点名要人工，或提到退款 / 投诉，需要你亲自处理",
                    item_info=item_info, buyer_has_order=buyer_has_order)
                return

            # 获取完整的对话上下文
            context = self.context_manager.get_context_by_chat(chat_id)
            # 生成回复（大模型调用放线程里，别卡住心跳）
            bot_reply = await asyncio.to_thread(
                self.bot.generate_reply,
                send_message,
                item_description,
                context,
                buyer_has_order,
            )

            # 拿不准、没写出内容、或模型标了转人工：不要把猜测发给买家
            if not (bot_reply or '').strip() or looks_uncertain(bot_reply):
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                stuck = "模型判断自己答不了，硬答容易编"
                if (bot_reply or '').strip() and "转人工" not in bot_reply:
                    stuck = f"回复像在猜（「{(bot_reply or '')[:40]}」），已拦住不发给买家"
                elif not (bot_reply or '').strip():
                    stuck = "模型没有给出回复，不能空着发，也不能现编"
                await self.handoff(
                    websocket, chat_id, send_user_id, send_user_name, item_id, send_message, stuck,
                    item_info=item_info, buyer_has_order=buyer_has_order)
                return
            
            # 检查是否需要回复
            if bot_reply == "-":
                logger.info(f"[无需回复] 用户 {send_user_name} 的消息被识别为无需回复类型")
                return

            bot_reply, dropped = guard_ordered_reply(bot_reply, buyer_has_order)
            if dropped:
                logger.info(f"买家已拍下商品 {item_id}，拦下催拍话 {dropped}，发出：{bot_reply}")
            
            # 添加用户消息到上下文
            self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
            
            # 检查是否为价格意图，如果是则增加议价次数
            if self.bot.last_intent == "price":
                self.context_manager.increment_bargain_count_by_chat(chat_id)
                bargain_count = self.context_manager.get_bargain_count_by_chat(chat_id)
                logger.info(f"用户 {send_user_name} 对商品 {item_id} 的议价次数: {bargain_count}")

            # 添加机器人回复到上下文
            self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", bot_reply)
            
            logger.info(f"机器人回复: {bot_reply}")
            
            # 模拟人工输入延迟
            if self.simulate_human_typing:
                # 基础延迟 2-6秒（像真人看完消息再打字）+ 每字 0.15-0.35秒
                base_delay = random.uniform(2, 6)
                typing_delay = len(bot_reply) * random.uniform(0.15, 0.35)
                total_delay = base_delay + typing_delay
                # 设置最大延迟上限，防止过长回复等待太久
                total_delay = min(total_delay, 15.0)
                
                logger.info(f"模拟人工输入，延迟发送 {total_delay:.2f} 秒...")
                await asyncio.sleep(total_delay)
                
            await self.send_msg(websocket, chat_id, send_user_id, bot_reply)
            
        except Exception as e:
            logger.exception(f"处理消息时发生错误: {e!r}")
            logger.debug(f"原始消息: {message_data}")
            await asyncio.to_thread(
                notifier.alert, f"bot_error:{self.account}",
                f"闲鱼自动回复：{self.account} 出错了",
                f"处理买家消息时出错，这条消息没有自动回复，请看一下：{e!r}", 1800)

    # ---------- 发消息：图片 / 文件 ----------
    async def _send_custom(self, ws, cid, toid, payload, summary=None, custom_type=1):
        data_b64 = str(base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 'utf-8')
        custom = {"type": custom_type, "data": data_b64}
        if summary is not None:
            custom.update({"summary": summary, "title": "", "degrade": ""})
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": generate_mid()},
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {"contentType": 101, "custom": custom},
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {"actualReceivers": [f"{toid}@goofish", f"{self.myid}@goofish"]}
            ]
        }
        await ws.send(json.dumps(msg))

    async def send_image(self, ws, cid, toid, path):
        url, w, h, _ = await asyncio.to_thread(self.xianyu.upload_media, path)
        if not (w and h):
            w, h = media.image_size(path)
        payload = {"contentType": 2, "image": {"pics": [{"height": int(h), "type": 0, "url": url, "width": int(w)}]}}
        await self._send_custom(ws, cid, toid, payload)

    async def send_video_as_file(self, ws, cid, toid, path):
        f = await asyncio.to_thread(self.xianyu.upload_file_oss, cid, self.myid, path)
        payload = {"atUsers": [], "contentType": 33, "file": f}
        await self._send_custom(ws, cid, toid, payload, summary=f"[文件]{f['displayName']}", custom_type=33)

    # ---------- 素材 ----------
    async def has_order(self, buyer_id, item_id):
        order = await asyncio.to_thread(
            self.xianyu.find_order, buyer_id, item_id, ('NOT_PAY', 'NOT_SHIP', 'SHIPPED', 'TRADE_SUCCESS'))
        return order is not None

    async def send_media(self, ws, chat_id, buyer_id, item_id):
        """把这个商品素材文件夹里的图片和视频发给买家；发完记下来，不重复发"""
        folder = media.item_folder(item_id)
        imgs, vids = media.list_media(folder) if folder else ([], [])
        logger.info(f"给买家 {buyer_id} 发商品 {item_id} 的素材：{len(imgs)} 张图 {len(vids)} 个视频")
        for path in imgs:
            await self.send_image(ws, chat_id, buyer_id, path)
            await asyncio.sleep(random.uniform(1.0, 2.5))
        for path in vids:
            await self.send_video_as_file(ws, chat_id, buyer_id, path)
            await asyncio.sleep(random.uniform(1.0, 2.5))
        self.store.media_sent(chat_id, item_id)

    async def handle_media_request(self, ws, chat_id, buyer_id, buyer_name, item_id, text, buyer_has_order=None, item_info=None):
        folder = media.item_folder(item_id)
        imgs, vids = media.list_media(folder) if folder else ([], [])
        if buyer_has_order is True:
            has_order = True
        else:
            has_order = (not self.media_require_order) or await self.has_order(buyer_id, item_id)
        if not has_order:
            # 没拍下：直接回拍下后发，不拖；素材文件夹还空着就顺手提醒机主（不暂停会话）
            self.store.want_media(chat_id, buyer_id, item_id)
            await self.reply_text(ws, chat_id, buyer_id, random.choice(NEED_ORDER_REPLIES if re.search('购买|采购|进货|凭证|订单|官网', text) else NEED_ORDER_REPLIES_OTHER), item_id)
            if not imgs and not vids:
                await self.notify_empty_media(buyer_name, item_id, text, folder)
            return
        if not imgs and not vids:
            await self.handoff(ws, chat_id, buyer_id, buyer_name, item_id, text,
                               f"买家要购买记录 / 实物素材，但「素材\\{os.path.basename(folder or str(item_id))}」文件夹里还没放文件",
                               key=f"media_empty:{self.account}:{item_id}", cooldown=86400,
                               item_info=item_info, buyer_has_order=buyer_has_order)
            return
        if self.store.media_already_sent(chat_id, item_id):
            logger.info(f"商品 {item_id} 的素材已经发过给会话 {chat_id}，不重复发")
            await self.reply_text(ws, chat_id, buyer_id, '宝宝刚发你了 往上翻下', item_id)
            return
        await self.send_media(ws, chat_id, buyer_id, item_id)
        await self.reply_text(ws, chat_id, buyer_id, '发你了宝宝', item_id)

    async def notify_empty_media(self, buyer_name, item_id, text, folder):
        logger.info(f"素材文件夹为空，只提醒机主：{item_id}")
        await asyncio.to_thread(
            notifier.alert, f"media_empty:{self.account}:{item_id}",
            f"闲鱼[{self.account}]有买家要购买记录",
            f"买家：{buyer_name}\n商品：{item_id}\n他说：{text}\n「素材\\{os.path.basename(folder or str(item_id))}」文件夹里还没放文件，放进去以后这个商品的买家拍下就自动收到。已经让他先拍下。",
            86400)

    async def reply_text(self, ws, chat_id, buyer_id, text, item_id=''):
        """像真人一样隔几秒再发一句文字，并记进聊天记录"""
        await asyncio.sleep(random.uniform(2, 5))
        self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", text)
        await self.send_msg(ws, chat_id, buyer_id, text)

    def load_item_info(self, item_id):
        """读商品详情，并把卖家 id 一并存上。卖家 id 在 sellerDO，不在 itemDO 里。"""
        cached = self.context_manager.get_item_info(item_id)
        if cached and (explain_seller_fields(cached)[0] or cached.get('_seller_checked')):
            logger.info(f"从数据库获取商品信息: {item_id}")
            return cached
        if time.time() - self._item_fetch_fail.get(str(item_id), 0) < 300:
            logger.info(f"商品 {item_id} 详情刚才没拉到，5 分钟内不再反复请求")
            return cached
        logger.info(f"从API获取商品信息: {item_id}" + ("（缓存里没有可用卖家ID，重新拉）" if cached else ""))
        try:
            api_result = self.xianyu.get_item_info(item_id)
        except Exception as e:
            logger.warning(f"获取商品信息失败 {item_id}：{e!r}")
            self._item_fetch_fail[str(item_id)] = time.time()
            return cached
        data = (api_result or {}).get('data') if isinstance(api_result, dict) else None
        item_do = (data or {}).get('itemDO') if isinstance(data, dict) else None
        if not item_do:
            logger.warning(f"获取商品信息失败: {api_result}")
            self._item_fetch_fail[str(item_id)] = time.time()
            return cached
        item = attach_seller_id(item_do, data)
        sid, rejected = explain_seller_fields(item)
        if sid:
            logger.info(f"商品 {item_id} 卖家ID {sid}")
        else:
            logger.warning(f"商品 {item_id} 详情里没有完整卖家ID，丢弃的字段：{rejected or '无'}")
        self.context_manager.save_item_info(item_id, item)
        return item

    async def _my_item_index(self):
        """账号主页商品 id。拉失败返回 None，不能当成空列表（空列表会被误当成「都不是我的」）。"""
        now = time.time()
        if self._my_items_cache is not None and now - self._my_items_at < 600:
            ids, note = self._my_items_cache
            return ids, note + '（10分钟内的缓存）'
        try:
            items = await asyncio.to_thread(self.xianyu.list_my_items)
        except Exception as e:
            logger.warning(f"拉账号商品列表失败，不能用列表判断是不是买家：{e!r}")
            return None
        ids = {str(it['item_id']) for it in items}
        ids |= {str(x) for x in getattr(self.xianyu, 'last_item_id_aliases', set())}
        note = getattr(self.xianyu, 'last_item_list_note', f'拉到 {len(items)} 个')
        self._my_items_cache = (ids, note)
        self._my_items_at = now
        return ids, note

    async def seller_role(self, item_id, item_info):
        """本号在这个会话商品上是卖家、买家，还是还不能确定。"""
        sid, rejected = explain_seller_fields(item_info or {})
        in_list = None
        list_note = ''
        if not sid:
            index = await self._my_item_index()
            if index is None:
                list_note = '商品列表没拉到'
            else:
                ids, list_note = index
                in_list = str(item_id) in ids
        role, why = classify_role(self.myid, sid, in_list)
        if not sid and rejected:
            why += f"。详情里这些字段不能当卖家ID：{rejected}"
        if list_note and not sid:
            why += f"。列表情况：{list_note}"
        return role, why

    def remember_owner_reply(self, chat_id, item_id, owner_text):
        """人工接管时，机主怎么回，就记成以后同类问题的话术。"""
        text = (owner_text or '').strip()
        if not text or self.check_toggle_keywords(text) or is_closing(text) or text in STALL_REPLIES:
            return
        if not self.is_manual_mode(chat_id):
            return
        question = ''
        for msg in reversed(self.context_manager.get_context_by_chat(chat_id)):
            if msg.get('role') == 'user':
                question = msg.get('content') or ''
                break
        if not question or is_closing(question):
            logger.info("人工回复没有对应的买家问题，不记话术")
            return
        saved = self.scripts.learn(question, text, item_id=item_id, account=self.account)
        if saved:
            logger.info(f"记下话术（{saved}）：问「{question}」→「{text}」")

    # ---------- 转人工 ----------
    async def handoff(self, ws, chat_id, buyer_id, buyer_name, item_id, text, reason, key=None, cooldown=600,
                      item_info=None, buyer_has_order=None):
        """拿不准就交给机主：稳住买家，暂停这个会话，通知里写清原话、卡在哪、两条可复制的回复。"""
        self.enter_manual_mode(chat_id)
        info = item_info if isinstance(item_info, dict) else (self.context_manager.get_item_info(item_id) or {})
        title = info.get('title') or ''
        desc = info.get('desc') or ''
        stuck = explain_stuck(text, reason, f"{title}\n{desc}")
        drafts = suggest_replies(text, title, desc, has_order=buyer_has_order)
        points = summarize_chat(self.context_manager.get_context_by_chat(chat_id))
        notice_title, notice_body = build_handoff_notice(
            self.account, buyer_name, item_id, title, text, stuck, points, drafts)
        logger.info(f"🔴 转人工：{buyer_name}（{buyer_id}）商品 {item_id}\n{notice_body}")
        await self.reply_text(ws, chat_id, buyer_id, random.choice(STALL_REPLIES), item_id)
        await asyncio.to_thread(
            notifier.alert, key or f"human:{self.account}:{chat_id}",
            notice_title, notice_body, cooldown, True)

    # ---------- 素材文件夹：所有在售商品提前建好 ----------
    async def media_sync_loop(self):
        """启动后 20 秒、之后每 30 分钟：拉本账号在售商品，缺文件夹的建上（已有的不动、下架的不删）"""
        await asyncio.sleep(20)
        while True:
            try:
                items = await asyncio.to_thread(self.xianyu.list_my_items)
                made = 0
                for it in items:
                    if not media.item_folder(it['item_id']):
                        media.ensure_item_folder(it['item_id'], it['title'])
                        made += 1
                logger.info(f"[{self.account}] 素材文件夹同步：在售 {len(items)} 个商品，新建 {made} 个")
            except Exception as e:
                logger.error(f"[{self.account}] 素材文件夹同步失败：{e!r}")
            await asyncio.sleep(1800)

    # ---------- 订单：自动改价 + 拍下后发素材 ----------
    def spawn_reconcile(self, ws):
        if self._reconciling:
            return
        self._reconciling = True
        asyncio.create_task(self.reconcile_orders(ws))

    async def reconcile_orders(self, ws):
        try:
            for delay in (3, 12, 40):
                await asyncio.sleep(delay)
                try:
                    await self._reconcile_once(ws)
                except Exception as e:
                    logger.exception(f"处理订单出错：{e!r}")
                    await asyncio.to_thread(
                        notifier.alert, f"order_error:{self.account}", f"闲鱼自动回复：{self.account} 处理订单出错",
                        f"自动改价 / 发素材时出错，请手动看一下订单：{e!r}", 1800)
                    return
        finally:
            self._reconciling = False

    async def _reconcile_once(self, ws):
        if self.auto_price:
            for o in await asyncio.to_thread(self.xianyu.list_sold_orders, 'NOT_PAY'):
                await self._maybe_adjust(ws, o)
        for st in ('NOT_PAY', 'NOT_SHIP'):
            for o in await asyncio.to_thread(self.xianyu.list_sold_orders, st):
                if self.store.pending_media(o['buyer_id'], o['item_id']):
                    chat_id = self.store.pending_media(o['buyer_id'], o['item_id'])[0]
                    await self.send_media(ws, chat_id, o['buyer_id'], o['item_id'])
                    await self.reply_text(ws, chat_id, o['buyer_id'], '发你了宝宝', o['item_id'])

    async def _maybe_adjust(self, ws, order):
        oid = order['order_id']
        if not oid or self.store.price_done(oid):
            return
        chat_id = self.store.chat_of(order['buyer_id'], order['item_id'])
        if not chat_id:
            return
        context = self.context_manager.get_context_by_chat(chat_id)
        if self._price_checked.get(oid) == len(context):
            return
        self._price_checked[oid] = len(context)
        item_info = self.context_manager.get_item_info(order['item_id']) or {}
        agreed = await asyncio.to_thread(
            self.bot.extract_agreed_price, self.bot.format_history(context),
            self.build_item_description(item_info) if item_info else '', order['total_price'])
        total = float(order['total_price'] or 0)
        logger.info(f"订单 {oid}：订单金额 {total}，聊天里谈定的价格 {agreed}")
        if agreed is None or abs(agreed - total) < 0.005:
            return
        if agreed < total * self.min_price_ratio or agreed > total:
            await asyncio.to_thread(
                notifier.alert, f"price:{oid}", f"闲鱼[{self.account}]有订单需要你改价",
                f"买家 {order['buyer_nick']} 拍下订单 {oid}，订单金额 {total} 元，聊天里谈的是 {agreed} 元，"
                f"不在自动改价范围（标价的 {int(self.min_price_ratio * 100)}% 到 100%），请你手动处理。", 3600)
            self.store.mark_price_done(oid, -1)
            return
        before, after = await asyncio.to_thread(self.xianyu.modify_order_price, oid, agreed)
        self.store.mark_price_done(oid, agreed)
        logger.info(f"✅ 订单 {oid} 已自动改价：{before} → {after}")
        await self.reply_text(ws, chat_id, order['buyer_id'], random.choice(PRICE_DONE_REPLIES), order['item_id'])

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            logger.error(f"发送心跳包失败: {e}")
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)
                
                # 检查上次心跳响应时间，如果超时则认为连接已断开
                if (current_time - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，可能连接已断开")
                    break
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"心跳循环出错: {e}")
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if (
                isinstance(message_data, dict)
                and "headers" in message_data
                and "mid" in message_data["headers"]
                and "code" in message_data
                and message_data["code"] == 200
            ):
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {e}")
        return False

    async def _connection_failed(self, why):
        """连续连不上闲鱼 5 次就通知机主（正常的整点重连不算，成功注册会清零）"""
        if self.connection_restart_flag:
            return
        self.fail_streak += 1
        if self.fail_streak >= 5:
            await asyncio.to_thread(
                notifier.alert, f"conn:{self.account}", f"闲鱼自动回复：{self.account} 连不上闲鱼",
                f"已经连续 {self.fail_streak} 次连接失败（{why}）。检查电脑网络；如果网络正常，大概率是登录过期，请双击「换登录.bat」重新扫码。", 3600)

    async def main(self):
        if not getattr(self, '_media_sync_started', False):
            self._media_sync_started = True
            asyncio.create_task(self.media_sync_loop())
        while True:
            try:
                # 重置连接重启标志
                self.connection_restart_flag = False
                
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    self.fail_streak = 0
                    
                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    
                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    
                    # 启动token刷新任务
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                    
                    async for message in websocket:
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break
                                
                            message_data = json.loads(message)
                            
                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue
                            
                            # 发送通用ACK响应
                            if "headers" in message_data and "mid" in message_data["headers"]:
                                ack = {
                                    "code": 200,
                                    "headers": {
                                        "mid": message_data["headers"]["mid"],
                                        "sid": message_data["headers"].get("sid", "")
                                    }
                                }
                                # 复制其他可能的header字段
                                for key in ["app-key", "ua", "dt"]:
                                    if key in message_data["headers"]:
                                        ack["headers"][key] = message_data["headers"][key]
                                await websocket.send(json.dumps(ack))
                            
                            # 处理其他消息
                            await self.handle_message(message_data, websocket)
                                
                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error(f"处理消息时发生错误: {str(e)}")
                            logger.debug(f"原始消息: {message}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                await self._connection_failed("WebSocket 连接被关闭")

            except Exception as e:
                logger.error(f"连接发生错误: {e}")
                await self._connection_failed(f"连接出错：{e!r}")
                
            finally:
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                        
                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass
                
                # 如果是主动重启，立即重连；否则等待5秒
                if self.connection_restart_flag:
                    logger.info("主动重启连接，立即重连...")
                else:
                    logger.info("等待5秒后重连...")
                    await asyncio.sleep(5)



def acquire_lock(account_dir):
    """一个账号同一时间只许一个程序在跑（重复启动会让买家收到两遍回复）。进程结束系统自动释放。"""
    os.makedirs(account_dir, exist_ok=True)
    f = open(os.path.join(account_dir, '.lock'), 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main():
    """python main.py <账号名>   账号名对应 accounts\\<账号名>\\.env（里面有这个号自己的 COOKIES_STR）"""
    account = sys.argv[1] if len(sys.argv) > 1 else '主号'
    account_dir = os.path.join(BASE_DIR, 'accounts', account)
    os.chdir(BASE_DIR)

    # 共用设置在根目录 .env（模型、通知），账号自己的 .env 覆盖它
    load_dotenv(os.path.join(BASE_DIR, '.env'))
    load_dotenv(os.path.join(account_dir, '.env'), override=True)
    load_dotenv(os.path.join(BASE_DIR, '.env.example'))  # 不会覆盖已存在的变量

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:MM-DD HH:mm:ss}</green> | <level>{level: <7}</level> | [" + account + "] {message}"
    )
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    logger.add(os.path.join(BASE_DIR, 'logs', f'{account}.log'), level='DEBUG', rotation='5 MB', retention=3, encoding='utf-8')
    logger.info(f"账号「{account}」启动")
    cleanup_claims()
    lock_file = acquire_lock(account_dir)
    if lock_file is None:
        logger.error(f"账号「{account}」已经有一个程序在跑了，这次不重复启动")
        sys.exit(3)

    # 缺的配置直接报清楚然后退出；退出码 3 表示「重启也没用」，启动器不会反复拉起
    if not os.getenv("API_KEY"):
        logger.error("根目录 .env 里没有 API_KEY（模型密钥）")
        sys.exit(3)
    cookies_str = os.getenv("COOKIES_STR", "")
    if not cookies_str or cookies_str == "your_cookies_here" or 'unb=' not in cookies_str:
        logger.error(f"账号「{account}」还没有登录信息：请双击「添加账号.bat」扫码登录")
        sys.exit(3)

    bot = XianyuReplyBot()
    live = XianyuLive(cookies_str, account=account, account_dir=account_dir, bot=bot)
    asyncio.run(live.main())


if __name__ == '__main__':
    main()
