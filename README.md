# Bybit WebSocket
[bybit WebSocket](https://bybit-exchange.github.io/docs/inverse/#t-websocket)に接続してデータ取得＆管理を行います.

## 必要パッケージ インストール
`pip install -U -r requirements.txt`

## BybitWSクラス
コンストラクタ引数に**API情報, 接続先, 通貨ペア, 購読するチャンネルリスト, チャンネル別のコールバック関数dict**を指定してインスタンスを生成してください.
```
# Bybit WebSocketインスタンス生成
bybit_ws = BybitWS('API_KEY', 'API_SECRET', is_testnet=False, symbol='BTCUSD', channel=[], callback={})
```
<br>
**購読するチャンネルリスト**には使用するチャンネルを指定してください.<br>
(省略すると以下のdefaultチャンネルを購読します.)
```
channel_list = [
    'trade.' + symbol,
    'instrument_info.100ms.' + symbol,
    'orderBook_200.100ms.' + symbol,
    'klineV2.' + period + '.' + symbol,
    'position',
    'execution',
    'order',
]
```
<br>
**チャンネル別のコールバック関数dict**は各チャンネルのデータ受信をトリガーとして呼び出される関数を設定します.<br>
dictの**key**に**チャンネルを示すtopic**, **value**に**コールバック関数**を指定してください.<br>
(dictに未設定またはvalueがNoneのチャンネルはコールバックされません.)
```
callback = {
    'trade'     : None,               # Noneはコールバックなし
    'instrument': None,               # Noneはコールバックなし
    'ohlcv'     : callback_ohlcv,     # ohlcv受信でcallback_ohlcv関数を呼び出し
    'position'  : callback_position,  # position受信でcallback_position関数を呼び出し
    'execution' : callback_execution, # execution受信でcallback_execution関数を呼び出し
    'order'     : callback_order,     # order受信でcallback_order関数を呼び出し
}
```
<br>
**コールバック関数**は引数に**BybitWSインスタンス, 受信データ**を設定してください.
```
#-------------------------------------------------------------------------------
# ex) 自注文の約定データ受信時に呼び出される関数
#-------------------------------------------------------------------------------
def callback_execution(ws:BybitWS, data:dict):
    msg = f'[execution]\n'
    for o in data:
        price = float(o['price'])
        qty = int(o['exec_qty'])
        odr = int(o['order_qty'])
        lvs = int(o['leaves_qty'])
        fee = float(o['exec_fee'])
        msg += f"  Type   : {o['exec_type']}\n"
        msg += f"  Side   : {o['side']}\n"
        msg += f"  Price  : {price:.1f}\n"
        msg += f"  Qty    : {qty} ({odr-lvs} / {odr})\n"
        msg += f"  Fee    : {fee:.8f}\n"
        msg += f"  Time   : {o['trade_time']}\n"
        msg += '\n'
    print(msg)
```
<br>
**BybitWS**インスタンスで受信したデータは**data**に格納されます.<br>
必要なデータを参照してください.
```
# 受信データ格納dict
data = {
    'connection':False,
    'last_price':0,
    'timestamp':{},
    'ohlcv':deque(maxlen=1000),
    'execution':deque(maxlen=200),
    'instrument':{},
    'board_snapshot':{
            'asks':[],
            'bids':[]
            },
    'position':{},
    'my_execution':deque(maxlen=50),
    'my_order':deque(maxlen=50),
    'my_open_order':{},
}
```
orderbookは更新頻度が高いため, 取得する場合は**get_orderbooks関数**を使用してください.<br>
排他制御にて安全にデータ取得します.
```
#---------------------------------------------------------------------------
# orderbook取得
#---------------------------------------------------------------------------
def get_orderbooks(self):
    with self.__lock:
        bids = copy.copy(self.data['board_snapshot']['bids'])
        asks = copy.copy(self.data['board_snapshot']['asks'])
    return {'bids':bids, 'asks':asks}
```

## 状態通知botの使い方
**bybit_ws_notify.py**の1ファイルで完結しています.<br>
(シンプルに使用できるようBybitWSや必要なクラス, 設定情報をあえて1ファイルに含めています.)<br>
pythonスクリプト内の冒頭にある**設定パラメータ**に必要情報を設定して起動してください.
```
#===============================================================================
# 設定パラメータ
#===============================================================================
# Discord送信
DISCORD_NOTIFY      = False   # True:送信する, False:送信しない
DISCORD_WEBHOOK_URL = ''

# bybit設定
BYBIT_API_KEY    = ''
BYBIT_API_SECRET = ''
BYBIT_IS_TESTNET = False      # True:testnet, False:real
BYBIT_SYMBOL     = 'BTCUSD'   # 通貨ペア

# 通知設定
BYBIT_ORDER_NOTIFY     = True # 注文に変更があると通知
BYBIT_EXECUTION_NOTIFY = True # 注文が約定すると通知
BYBIT_POSITION_NOTIFY  = True # ポジションや残高に変更があると通知
BYBIT_OHLCV_NOTIFY     = True # OHLCVに変更があると通知
BYBIT_PERIOD           = '1'  # ohlcv時間足 (1 3 5 15 30 60 120 240 360 D W M)
#===============================================================================
```

## Discord通知用 Webhook url発行
https://note.com/asim0613/n/n23073851a93c

## 参考文献
* [Python Bybit websocket](https://note.com/1one/n/n26615e8b5f76)
* [bybit api document](https://bybit-exchange.github.io/docs/inverse/#t-websocket)
