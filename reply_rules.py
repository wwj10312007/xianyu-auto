"""回复规矩：卖家身份、拿不准就转人工、记住机主话术、已拍下不许催拍。

不联网，方便单独检查。话术库放在 data/learned_scripts.db（各账号共用）。
帮抢进度这类跟某一单绑定的话，只在同一个商品上复用。
"""
import os
import re
import sqlite3
from datetime import datetime


def numeric_id(value):
    """完整的数字 id。打码的（1***7）和空的都不要，避免拿来比对本号。"""
    if value is None or isinstance(value, bool):
        return ''
    text = str(value).strip()
    if re.fullmatch(r'\d{5,}', text):
        return text
    return ''


def explain_seller_fields(item_info):
    """从商品详情里找卖家 id。

    闲鱼把卖家 id 放在 sellerDO.sellerId，itemDO 里经常没有；
    trackParams.sellerId 又常常是打码的。只认完整数字。
    返回 (卖家id或空, 被丢掉的字段说明列表)。
    """
    rejected = []
    if not isinstance(item_info, dict):
        return '', ['商品信息不是字典']

    found = ''

    def consider(label, value):
        nonlocal found
        if value in (None, '', 0, '0'):
            return
        sid = numeric_id(value)
        if sid:
            if not found:
                found = sid
            return
        rejected.append(f'{label}={value}')

    for key in ('_seller_id', 'sellerId', 'seller_id'):
        consider(key, item_info.get(key))
    for nest_key in ('sellerDO', 'seller', 'b2cSellerDO'):
        nest = item_info.get(nest_key)
        if isinstance(nest, dict):
            consider(f'{nest_key}.sellerId', nest.get('sellerId'))
            consider(f'{nest_key}.userId', nest.get('userId'))
    track = item_info.get('trackParams')
    if isinstance(track, dict):
        consider('trackParams.sellerId', track.get('sellerId'))
    return found, rejected


def item_seller_id(item_info):
    sid, _ = explain_seller_fields(item_info)
    return sid


def attach_seller_id(item_do, detail_data):
    """把详情接口里、和 itemDO 平级的 sellerDO 写进要保存的商品信息。"""
    item = dict(item_do or {})
    data = detail_data if isinstance(detail_data, dict) else {}
    sid = ''
    for nest_key in ('sellerDO', 'b2cSellerDO'):
        nest = data.get(nest_key) or {}
        if isinstance(nest, dict):
            sid = numeric_id(nest.get('sellerId') or nest.get('userId'))
            if sid:
                item[nest_key] = {'sellerId': sid}
                break
    if not sid:
        sid = item_seller_id(item)
    item['_seller_checked'] = '1'
    if sid:
        item['_seller_id'] = sid
    return item


def classify_role(myid, seller_id, in_my_list):
    """判断当前号在这个商品上是卖家、买家，还是还不能确定。

    in_my_list: True 在账号商品列表里，False 不在，None 列表没拉到。
    不在列表里不能当成买家：列表会翻页翻不全，帮抢商品也可能不在主页那一页。
    """
    my = str(myid or '').strip()
    sid = str(seller_id or '').strip()
    if sid and my and sid == my:
        return 'seller', f'商品卖家ID {sid} 和本号一致'
    if sid and my and sid != my:
        return 'buyer', f'商品卖家ID {sid}，本号 {my}，不是同一个'
    if in_my_list is True:
        return 'seller', '详情里没有可用卖家ID，但商品在账号商品列表里'
    if in_my_list is False:
        return 'unknown', '详情里没有可用卖家ID，商品也不在账号商品列表里。列表可能没翻全，或帮抢不在主页，不能据此当成买家'
    return 'unknown', '详情里没有可用卖家ID，账号商品列表也没拉到'


def should_skip_as_buyer(role):
    """只有确定是买家才跳过。不确定时继续回复，避免本店咨询被丢掉。"""
    return role == 'buyer'


# ---------- 已拍下不许催拍 ----------

URGE_RE = re.compile(
    r'(喜欢直接拍下?|喜欢就拍|直接拍下|直接拍|尽快下单|尽快拍下|尽快拍|'
    r'赶紧下单|赶紧拍|马上拍下|马上拍|立刻下单|立刻拍|'
    r'拍下就好|拍下就发|拍下吧|先拍下|现在就拍|今天拍下|今天拍|去拍下|拍下来|'
    r'拍下商品|拍下改价|直接下单|可以拍了|拍下哦|拍下呀|价妥可下单|'
    r'刷新一下?就?拍|拍下后就发|拍下后发|引导.{0,6}拍下|叫他拍)'
)

ORDERED_FALLBACK = '已经拍下了宝宝，我按订单安排发货，地址要改跟我说'


def guard_ordered_reply(text, has_order, fallback=ORDERED_FALLBACK):
    """买家已经拍下时，删掉催拍的话。整句都是催拍就换成发货/售后。

    has_order 不是 True（没拍下，或还没查到）时原样返回。
    返回 (可以发出去的话, 被拦下的催拍词列表)。
    """
    original = text or ''
    if has_order is not True:
        return original, []
    removed = URGE_RE.findall(original)
    cleaned = URGE_RE.sub('', original)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    cleaned = re.sub(r'^[，,。.!！？?\s]+|[，,。.!！？?\s]+$', '', cleaned)
    cleaned = re.sub(r'[，,]{2,}', '，', cleaned).strip()
    core = re.sub(r'[\s\W_]+', '', cleaned)
    if len(core) < 4:
        return fallback, removed or ['整句都是催拍']
    return cleaned, removed


# ---------- 拿不准 ----------

UNCERTAIN_RE = re.compile(
    r'转人工|不确定|不太清楚|不大清楚|没法确定|无法确定|不知道|说不准|拿不准|'
    r'怕说错|不敢保证|我确认一下|我核实一下|商品里没写|描述里没有|不好乱说|不能乱说|我猜'
)


def looks_uncertain(reply):
    """模型没把握，或自己标了转人工。空和「无需回复」不算。"""
    if not reply or reply.strip() == '-':
        return False
    return bool(UNCERTAIN_RE.search(reply))


# ---------- 给机主的通知 ----------

def explain_stuck(buyer_text, reason, item_blob=''):
    """人话说明机器人卡在哪、缺什么信息。"""
    extra = []
    blob = item_blob or ''
    text = buyer_text or ''
    if re.search(r'抢到|代拍|帮抢|拍到了', text) and not re.search(r'抢到|没抢到|未抢到', blob):
        extra.append('商品标题和描述里没写这单到底抢到没有，不能编。')
    if re.search(r'天猫|京东|淘宝', text) and not re.search(r'天猫|京东|淘宝|官网', blob):
        extra.append('商品信息里看不出是天猫、京东还是淘宝款。')
    if re.search(r'购买记录|凭证|对光|实物', text) and '素材' not in (reason or ''):
        extra.append('购买记录/对光不能靠猜，得你确认有没有、能不能发。')
    parts = [p for p in (reason, ''.join(extra)) if p]
    return '；'.join(parts) if parts else '这个问题没有把握，硬答容易编。'


def suggest_replies(buyer_text, item_title='', item_desc='', has_order=None):
    """给机主 1～2 条可以直接复制的回复。已拍下的草稿里不催拍。

    抢到了没有这种事机器人不知道，就给两种说法，让机主挑对的那条复制。
    """
    text = buyer_text or ''
    blob = f'{item_title or ""}\n{item_desc or ""}'
    ordered = has_order is True

    if re.search(r'退款|退货|投诉|维权|差评', text):
        return [
            '宝宝怎么了，你说下情况，能处理我马上处理',
            '可以退的，你在订单里申请退款，我这边同意',
        ]

    if re.search(r'抢到|代拍|帮抢|抢了吗|拍到了', text):
        got = bool(re.search(r'已抢到|抢到了', blob)) and not re.search(r'没抢到|未抢到|还在抢', blob)
        missed = bool(re.search(r'没抢到|未抢到', blob))
        if ordered:
            if got:
                return ['宝宝这单抢到了，我按你的订单安排发货', '地址要改的话现在跟我说']
            if missed:
                return ['这单没抢到，你申请退款我同意', '不好意思宝宝，钱按订单退给你']
            return [
                '宝宝这单抢到了，我按你的订单安排发货',
                '这单还没抢到，你申请退款我同意，不用担心',
            ]
        if got:
            return ['宝宝这个抢到了，你拍下后我发购买记录给你', '全新未拆，拍下我就按这个给你留']
        if missed:
            return ['这单没抢到，不用拍了，想退就跟我说', '不好意思没抢到，你要的话我帮你看下一场']
        return [
            '宝宝这个抢到了，你拍下后我发购买记录给你',
            '这单还在抢，抢到了告诉你；没抢到可以退',
        ]

    if re.search(r'购买记录|购买截图|凭证|对光|实物', text):
        if ordered:
            return [
                '宝宝你已经拍下了，我把购买记录发给你',
                '记录我找一下，找到马上发你',
            ]
        return [
            '宝宝怕别人盗图，拍下我就发购买记录',
            '对光和实物也是拍下后发你，东西是全新未拆的',
        ]

    if re.search(r'天猫|京东|淘宝|哪款|什么款|版本', text):
        found = [name for name in ('天猫', '京东', '淘宝', '官网') if name in blob]
        if found:
            return [
                f'宝宝这款是{found[0]}的，全新未拆',
                '你要是对的另一款跟我说，我帮你看有没有',
            ]
        return [
            '宝宝你要天猫款还是京东款，说一下我按那个回你',
            '页面没写清是哪一款，我不猜，你告诉我款式',
        ]

    if re.search(r'发货|什么时候发|几天到|何时发', text):
        if ordered:
            return [
                '已经拍下了宝宝，货到了隔一两天就发',
                '地址没问题的话我按订单发，要改地址跟我说',
            ]
        return [
            '还没拍的话，货到了直接转寄，大约两天内',
            '你拍下后我按这个订单给你发',
        ]

    if re.search(r'便宜|少点|砍|最低|优惠', text):
        if ordered:
            return [
                '你这单已经拍下了，价格按订单为准，我这边发货',
                '这单不好再改价了，有问题你说下我看售后',
            ]
        return [
            '宝宝预算说个数，我看能不能给你让',
            '太低的话真做不了，这款是全新未拆的',
        ]

    if ordered:
        return [
            '已经拍下了宝宝，我按订单处理，发货和地址有问题直接说',
            '你具体想问哪一点，我按订单给你说清楚',
        ]
    return [
        '宝宝这个我按页面说：全新未拆，到货后大约两天发',
        '你问的这点页面上没写死，补充一下我再准确回你',
    ]


def summarize_chat(messages, limit=6):
    """最近几句，给机主看会话要点。"""
    lines = []
    for msg in messages or []:
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue
        content = re.sub(r'\s+', ' ', msg.get('content') or '').strip()
        if not content:
            continue
        who = '买家' if role == 'user' else '已回'
        lines.append(f'{who}：{content[:80]}')
    if len(lines) > limit:
        lines = lines[-limit:]
    return '\n'.join(lines)


def build_handoff_notice(account, buyer_name, item_id, item_title, buyer_text, stuck, chat_points, drafts):
    """通知正文。建议回复放最上面，方便弹窗被截断时也能先看到。"""
    lines = ['建议直接复制一条发过去：']
    usable = [d.strip() for d in (drafts or []) if d and d.strip()][:2]
    if not usable:
        usable = ['宝宝这个我确认一下，马上回你']
    for i, draft in enumerate(usable, 1):
        lines.append(f'{i}. {draft}')
    lines.append('')
    lines.append(f'买家原话：{buyer_text or "（空）"}')
    who = buyer_name or '买家'
    lines.append(f'买家：{who}')
    title = (item_title or '').strip() or '（没拿到标题）'
    lines.append(f'商品：{title}（{item_id}）')
    lines.append(f'卡在哪：{stuck or "没有把握，硬答容易编"}')
    lines.append('会话要点：')
    lines.append(chat_points or '（这是开头，还没有更早的对话）')
    lines.append('')
    lines.append('你在闲鱼里怎么回，机器人会记住，以后同类问题按你的原话回。这个会话暂停自动回复 1 小时。')
    short = re.sub(r'\s+', ' ', buyer_text or '一句话').strip()[:18]
    notice_title = f'闲鱼[{account}]请你回：{short}'
    return notice_title, '\n'.join(lines)


# ---------- 话术库 ----------

_STOP = re.compile(r'(宝宝|老师|亲|在吗|请问|一下|能不能|可以吗|想问|这个|那个|你好|您好)')
_ITEM_SCOPED = re.compile(r'抢到|代拍|帮抢|单号|物流|买到了|拍到了|发出了吗|发了吗')
_FAMILIES = (
    ('抢到', ('抢到', '代拍', '帮抢', '拍到')),
    ('便宜', ('便宜', '砍价', '最低', '少点', '优惠')),
    ('发货', ('发货', '多久发', '几天发', '什么时候发')),
    ('记录', ('购买记录', '凭证', '对光', '实物')),
    ('退款', ('退款', '退货', '投诉')),
    ('款式', ('天猫', '京东', '淘宝', '哪款', '版本')),
)


def question_key(text):
    raw = re.sub(r'[\s\W_]+', '', text or '')
    raw = _STOP.sub('', raw)
    raw = raw.replace('有没有抢到', '抢到').replace('抢到了', '抢到').replace('抢到没', '抢到')
    raw = re.sub(r'[吗呢啊呀哦嘛]', '', raw)
    return raw


def _families(text):
    found = []
    for name, words in _FAMILIES:
        if any(word in text for word in words):
            found.append(name)
    return found


def similarity(left, right):
    """两句买家的话像不像同一类。传入原句或已经归一化的 key 都可以。"""
    a, b = question_key(left), question_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 2 and shorter in longer:
        return 0.9
    score = _jaccard(a, b)
    if _families(a) and _families(a) == _families(b):
        score = max(score, 0.78)
    return score


def _jaccard(a, b):
    def grams(text):
        if len(text) < 2:
            return {text}
        return {text[i:i + 2] for i in range(len(text) - 1)}
    ga, gb = grams(a), grams(b)
    union = ga | gb
    if not union:
        return 0.0
    return len(ga & gb) / len(union)


def script_scope(question):
    return 'item' if _ITEM_SCOPED.search(question or '') else 'global'


class ScriptBook:
    """机主说过的话。问法差不多时优先用这句，不再让模型现编。"""

    def __init__(self, db_path):
        self.db_path = db_path
        folder = os.path.dirname(db_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with self._conn() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS learned_scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                question_key TEXT NOT NULL,
                reply TEXT NOT NULL,
                scope TEXT NOT NULL,
                item_id TEXT,
                account TEXT,
                updated_at TEXT
            )''')

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def learn(self, question, reply, item_id='', account=''):
        question = (question or '').strip()
        reply = (reply or '').strip()
        if len(question_key(question)) < 2 or len(reply) < 2:
            return None
        scope = script_scope(question)
        hit = self.find(question, item_id, threshold=0.8)
        now = datetime.now().isoformat(timespec='seconds')
        with self._conn() as conn:
            if hit and hit['scope'] == scope and (scope == 'global' or str(hit.get('item_id') or '') == str(item_id)):
                # 同一类问题以机主最后一句为准
                conn.execute(
                    'UPDATE learned_scripts SET reply=?, question=?, question_key=?, account=?, updated_at=? WHERE id=?',
                    (reply, question, question_key(question), account, now, hit['id']))
                return 'update'
            conn.execute(
                'INSERT INTO learned_scripts (question, question_key, reply, scope, item_id, account, updated_at) VALUES (?,?,?,?,?,?,?)',
                (question, question_key(question), reply, scope, str(item_id or ''), account, now))
        return 'insert'

    def find(self, question, item_id='', threshold=0.72):
        key = question_key(question)
        if len(key) < 2:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT id, question, question_key, reply, scope, item_id, account, updated_at FROM learned_scripts'
            ).fetchall()
        best = None
        best_rank = None
        for row in rows:
            scope = row[4]
            row_item = row[5] or ''
            if scope == 'item' and str(row_item) != str(item_id):
                continue
            score = similarity(key, row[2])
            if score < threshold:
                continue
            rank = (score, 1 if scope == 'item' else 0)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best = {
                    'id': row[0],
                    'question': row[1],
                    'question_key': row[2],
                    'reply': row[3],
                    'scope': scope,
                    'item_id': row_item,
                    'account': row[6],
                    'updated_at': row[7],
                    'score': score,
                }
        return best
