"""每个账号自己的小账本（和聊天记录在同一个 sqlite 里）：谁要过素材、哪些订单已改过价。"""
import sqlite3


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        with self._conn() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS media_requests (
                chat_id TEXT, buyer_id TEXT, item_id TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                sent INTEGER DEFAULT 0, PRIMARY KEY (chat_id, item_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS price_done (
                order_id TEXT PRIMARY KEY, price REAL, ts DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def want_media(self, chat_id, buyer_id, item_id):
        with self._conn() as c:
            c.execute('INSERT OR IGNORE INTO media_requests (chat_id, buyer_id, item_id) VALUES (?,?,?)',
                      (chat_id, buyer_id, item_id))

    def pending_media(self, buyer_id, item_id):
        with self._conn() as c:
            return c.execute('SELECT chat_id FROM media_requests WHERE buyer_id=? AND item_id=? AND sent=0',
                             (buyer_id, item_id)).fetchone()

    def media_sent(self, chat_id, item_id):
        with self._conn() as c:
            c.execute('UPDATE media_requests SET sent=1 WHERE chat_id=? AND item_id=?', (chat_id, item_id))

    def media_already_sent(self, chat_id, item_id):
        with self._conn() as c:
            r = c.execute('SELECT sent FROM media_requests WHERE chat_id=? AND item_id=?', (chat_id, item_id)).fetchone()
            return bool(r and r[0])

    def price_done(self, order_id):
        with self._conn() as c:
            return c.execute('SELECT 1 FROM price_done WHERE order_id=?', (order_id,)).fetchone() is not None

    def mark_price_done(self, order_id, price):
        with self._conn() as c:
            c.execute('INSERT OR REPLACE INTO price_done (order_id, price) VALUES (?,?)', (order_id, price))

    def chat_of(self, buyer_id, item_id):
        """这个买家在这个商品上最近的会话 id"""
        with self._conn() as c:
            r = c.execute('SELECT chat_id FROM messages WHERE user_id=? AND item_id=? AND chat_id IS NOT NULL '
                          'ORDER BY id DESC LIMIT 1', (buyer_id, item_id)).fetchone()
            return r[0] if r else None
