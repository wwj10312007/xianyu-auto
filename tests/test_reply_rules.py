"""不联网，检查卖家身份、催拍拦截、转人工通知、话术库。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reply_rules import (
    ScriptBook, URGE_RE, attach_seller_id, build_handoff_notice, classify_role,
    explain_seller_fields, explain_stuck, guard_ordered_reply, item_seller_id,
    looks_uncertain, should_skip_as_buyer, suggest_replies,
)
from XianyuApis import collect_item_pages


ITEM = '1097693140277'
MY = '2200000001'
OTHER = '2200000002'


def test_masked_seller_id_is_not_buyer():
    """神仙鱼号那条日志：详情没有完整卖家ID，又不在（可能没翻全的）商品列表里。
    以前据此当成买家，把「抢到了吗」全跳过。现在必须当成不确定，继续回复。"""
    item = {'title': '帮抢周边', 'trackParams': {'sellerId': '1***7'}}
    sid, rejected = explain_seller_fields(item)
    assert sid == ''
    assert rejected
    role, why = classify_role(MY, sid, False)
    assert role == 'unknown'
    assert not should_skip_as_buyer(role)
    assert '不能据此当成买家' in why


def test_seller_do_decides():
    detail = {'sellerDO': {'sellerId': MY}, 'itemDO': {'title': '帮抢', 'trackParams': {'sellerId': '1***7'}}}
    saved = attach_seller_id(detail['itemDO'], detail)
    assert item_seller_id(saved) == MY
    assert classify_role(MY, item_seller_id(saved), False)[0] == 'seller'
    assert should_skip_as_buyer('seller') is False

    buyer = attach_seller_id({'title': '别人的'}, {'sellerDO': {'sellerId': int(OTHER)}})
    role, why = classify_role(MY, item_seller_id(buyer), True)
    assert role == 'buyer'
    assert should_skip_as_buyer(role)
    assert OTHER in why


def test_list_membership_is_only_positive():
    assert classify_role(MY, '', True)[0] == 'seller'
    assert classify_role(MY, '', None)[0] == 'unknown'


def test_item_list_second_page_uses_cursor():
    """第 2 页才有 1097693140277，而且卡片 id 和商品 id 不一样。必须带翻页标记，并以 detailParams.itemId 为准。"""
    calls = []

    def fetch(payload):
        calls.append(dict(payload))
        if payload['pageNumber'] == 1:
            return {
                'cardList': [{'cardData': {'id': '111', 'title': '别的', 'detailParams': {'itemId': '111'}}}],
                'nextPage': True,
                'nextPageModel': 'CURSOR',
                'nextPageNum': 2,
                'itemGroupList': [{'groupName': '综合', 'groupId': 9, 'defaultGroup': True, 'itemNumber': 2}],
            }
        return {
            'cardList': [{
                'cardData': {
                    'id': 'not-the-item',
                    'title': '卡片标题不要用',
                    'detailParams': {'itemId': ITEM, 'title': '帮抢小卡'},
                }
            }],
            'nextPage': False,
        }

    items, aliases, note = collect_item_pages(fetch, MY, sleep_fn=None)
    assert calls[1]['nextPageModel'] == 'CURSOR'
    assert calls[1]['groupId'] == 9
    ids = [it['item_id'] for it in items]
    assert ITEM in ids
    assert 'not-the-item' not in ids
    assert 'not-the-item' in aliases
    assert next(it['title'] for it in items if it['item_id'] == ITEM)
    assert '拉到 2 个' in note


def test_urge_blocked_only_after_order():
    mixed, removed = guard_ordered_reply('货到了就发，喜欢直接拍', True)
    assert '喜欢直接拍' not in mixed
    assert '货到了就发' in mixed
    assert removed

    for phrase in ('尽快下单', '拍下就好', '直接拍'):
        text, _ = guard_ordered_reply(f'{phrase}吧宝宝', True)
        assert phrase not in text
        assert '已经拍下了' in text

    kept, removed = guard_ordered_reply('喜欢直接拍，今天可以留', False)
    assert kept.startswith('喜欢直接拍')
    assert removed == []

    same, _ = guard_ordered_reply('尽快下单', None)
    assert same == '尽快下单'
    kept_ok, removed_ok = guard_ordered_reply('已经拍下了宝宝，我按订单安排发货', True)
    assert kept_ok == '已经拍下了宝宝，我按订单安排发货'
    assert removed_ok == []


def test_ordered_drafts_do_not_urge():
    drafts = suggest_replies('抢到了吗', '某小卡', '全新未拆', has_order=True)
    assert len(drafts) == 2
    blob = '\n'.join(drafts)
    assert not URGE_RE.search(blob)
    assert '尽快下单' not in blob and '喜欢直接拍' not in blob and '拍下就好' not in blob
    not_yet = suggest_replies('抢到了吗', has_order=False)
    assert any('拍下' in line for line in not_yet)


def test_handoff_notice_has_what_owner_needs():
    stuck = explain_stuck('抢到了吗', '模型判断自己答不了', '某小卡\n全新未拆')
    assert '没写这单到底抢到没有' in stuck
    drafts = suggest_replies('抢到了吗', '某小卡', '全新未拆', has_order=False)
    title, body = build_handoff_notice('神仙鱼', '小王', ITEM, '某小卡', '抢到了吗', stuck, '买家：抢到了吗', drafts)
    assert '请你回' in title
    assert '买家原话：抢到了吗' in body
    assert ITEM in body and '某小卡' in body
    assert '卡在哪：' in body
    assert '会话要点：' in body
    assert '1. ' in body and '2. ' in body
    assert '买家等你回复' not in body


def test_uncertain_marker():
    assert looks_uncertain('[转人工]')
    assert looks_uncertain('这个我不确定')
    assert not looks_uncertain('-')
    assert not looks_uncertain('全新未拆，到货两天左右发')


def test_script_book_reuses_owner_words():
    path = os.path.join(tempfile.mkdtemp(), 'learned.db')
    book = ScriptBook(path)
    assert book.learn('抢到了吗', '宝宝这个抢到了，拍下后发记录', item_id=ITEM, account='神仙鱼') == 'insert'
    hit = book.find('宝宝这个抢到了吗', ITEM)
    assert hit and hit['reply'] == '宝宝这个抢到了，拍下后发记录'
    assert book.find('抢到没', '另一个商品') is None
    assert book.learn('一般几天发货', '到货后大约两天就发', item_id=ITEM) == 'insert'
    other = book.find('什么时候发货', '另一个商品')
    assert other and '两天' in other['reply']
    assert book.learn('抢到了吗', '这单还在抢，没抢到可以退', item_id=ITEM) == 'update'
    assert '还在抢' in book.find('抢到了吗', ITEM)['reply']


def test_prompts_match_new_style():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default = open(os.path.join(root, 'prompts', 'default_prompt.txt'), encoding='utf-8').read()
    price = open(os.path.join(root, 'prompts', 'price_prompt.txt'), encoding='utf-8').read()
    tech = open(os.path.join(root, 'prompts', 'tech_prompt.txt'), encoding='utf-8').read()
    classify = open(os.path.join(root, 'prompts', 'classify_prompt.txt'), encoding='utf-8').read()
    assert '不超过15' not in default
    assert '音响' not in default and '音响' not in tech
    assert '音响' in price and '一律忽略' in price
    assert '[转人工]' in default and '[转人工]' in tech and '[转人工]' in price
    assert '已经拍下' in default
    assert 'price / tech / default / no_reply' in classify
    agent = open(os.path.join(root, 'XianyuAgent.py'), encoding='utf-8').read()
    assert '不要因为没把握就转人工' not in agent
    assert '买家已经拍下或已经付款' in agent
    main = open(os.path.join(root, 'main.py'), encoding='utf-8').read()
    assert 'STRIP_PUNCT' not in main


if __name__ == '__main__':
    tests = [v for name, v in sorted(globals().items()) if name.startswith('test_')]
    failed = 0
    for fn in tests:
        try:
            fn()
            print('ok', fn.__name__)
        except Exception as e:
            failed += 1
            print('FAIL', fn.__name__, e)
    if failed:
        raise SystemExit(1)
    print(f'{len(tests)} passed')
