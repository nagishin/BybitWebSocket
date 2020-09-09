# -*- coding: utf-8 -*-
import os
import hmac
import hashlib
import json
import logging
import logging.handlers
import websocket
import threading
import requests
import queue
import traceback
import copy
from time import time, sleep
from datetime import datetime
from pytz import timezone
from collections import deque
from sortedcontainers import SortedDict
from pprint import pprint


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


#===============================================================================
# 通知管理クラス
#===============================================================================
class Notify(object):

    DISCORD_URL = ''
    __loggers = {}

    #---------------------------------------------------------------------------
    # Discord送信
    #---------------------------------------------------------------------------
    # [@param]
    #     message      送信するメッセージ
    #     fileName     送信するファイルパス
    # [return]
    #---------------------------------------------------------------------------
    @classmethod
    def discord_notify(cls, message:str='', fileName:str=None):
        if len(cls.DISCORD_URL) > 0:
            discord_webhook_url = cls.DISCORD_URL
            data = {'content': ' ' + message + ' '}
            if fileName == None:
                try:
                    return requests.post(discord_webhook_url, data=data)
                except Exception:
                    print(traceback.format_exc())
            else:
                try:
                    files = {'imageFile': open(fileName, 'rb')}
                    return requests.post(discord_webhook_url, data=data, files=files)
                except Exception:
                    print(traceback.format_exc())
        return False

    #---------------------------------------------------------------------------
    # logger取得
    #---------------------------------------------------------------------------
    # [@param]
    #     name         logger識別名
    # [return]
    #     logger
    #---------------------------------------------------------------------------
    @classmethod
    def get_custom_logger(cls, name:str):
        base_dir = os.path.realpath(os.path.dirname(__file__))
        log_dir = os.path.join(base_dir, 'logs')  # ログファイルディレクトリ

        # ログファイルディレクトリがなければ作成する
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)

        if name is None:
            name = __name__

        if cls.__loggers is None:
            cls.__loggers = {}
        if cls.__loggers.get(name):
            return cls.__loggers.get(name)

        logger = logging.getLogger(name)
        # 出力フォーマット
        fileFormatter = logging.Formatter(
            fmt='[%(levelname)s] %(asctime)s %(module)s: %(message)s', datefmt='%m-%d %H:%M:%S')
        stdFormatter = logging.Formatter(
            fmt='[%(levelname)-5s] %(asctime)s %(module)s:L%(lineno)d : %(message)s', datefmt='%m-%d %H:%M:%S')
        # ログ用ハンドラー: コンソール出力用
        log_stream_handler = logging.StreamHandler()
        log_stream_handler.setFormatter(stdFormatter)
        log_stream_handler.setLevel(logging.DEBUG)

        # ログ用ハンドラー: ファイル出力用
        log_file_handler = logging.handlers.RotatingFileHandler(
            filename=os.path.join(log_dir, name + '.log'),
            maxBytes=1024 * 1024 * 2,
            backupCount=3
        )
        log_file_handler.setFormatter(fileFormatter)
        log_file_handler.setLevel(logging.DEBUG)

        # ロガーにハンドラーとレベルをセット
        logger.setLevel(logging.DEBUG)
        logger.addHandler(log_stream_handler)
        logger.addHandler(log_file_handler)

        cls.__loggers[name] = logger
        return logger


#===============================================================================
# bybit WebSocketクラス
#===============================================================================
class BybitWS(object):

    #---------------------------------------------------------------------------
    # コンストラクタ
    #---------------------------------------------------------------------------
    # [@param]
    #     api_key      API KEY
    #     secret       API SECRET
    #     is_testnet   True:testnet, False:real
    #     symbol       通貨ペア
    #     channel      購読するチャンネルリスト
    #     callback     チャンネル別のコールバック関数dict
    # [return]
    #---------------------------------------------------------------------------
    def __init__(self, api_key:str, secret:str, is_testnet:bool=False, symbol:str='BTCUSD', channel:list=[], callback:dict={}):
        # logger設定
        self.logger = Notify.get_custom_logger(self.__class__.__name__)
        self.logger.setLevel(20) # Level 10:debug 20:info
        self.logger.info('Initializing WebSocket...')

        self.api_key = api_key
        self.secret = secret
        self.symbol = symbol
        if is_testnet:
            self.endpoint = 'wss://stream-testnet.bybit.com/realtime'
        else:
            self.endpoint = 'wss://stream.bybit.com/realtime'
        self.period = BYBIT_PERIOD
        self.last_ohlcv = []

        # 購読チャンネル設定
        if len(channel) > 0:
            self.channel_list = channel
        else:
            self.channel_list = [
                'trade.' + self.symbol,
                'instrument_info.100ms.' + self.symbol,
                'orderBook_200.100ms.' + self.symbol, #'orderBookL2_25.' + self.symbol,
                'klineV2.' + self.period + '.' + self.symbol,
                'position',
                'execution',
                'order',
            ]

        # コールバック設定
        self.callback = callback
        self.callback_queue = queue.Queue()
        if len(self.callback.keys()) > 0:
            # コールバックする場合はhandlerスレッド生成
            t = threading.Thread(target=self.__callback_event_handler)
            t.daemon = True
            t.start()

        # 受信データ格納dict
        self.data = {
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
        for i in self.channel_list:
            self.data['timestamp'][i] = None

        self.board_snapshot_bids_dict = SortedDict()
        self.board_snapshot_asks_dict = SortedDict()
        self.__lock = threading.Lock() # 排他制御

        # WebSocket接続
        self.__connect(self.endpoint)

        # 定期ping/pongスレッド生成
        self.ping_th = threading.Thread(target=self.__send_ping)
        self.ping_th.daemon = True
        self.ping_th.start()

    #---------------------------------------------------------------------------
    # WebSocket接続
    #---------------------------------------------------------------------------
    def __connect(self, endpoint):
        while not self.data['connection']:
            self.ws = websocket.WebSocketApp(endpoint,
                                            on_message=self.__on_message,
                                            on_close=self.__on_close,
                                            on_open=self.__on_open,
                                            on_error=self.__on_error,
                                            header=None)

            self.logger.info('Connecting WebSocket...')

            self.ws_th = threading.Thread(target=lambda: self.ws.run_forever())
            self.ws_th.daemon = True
            self.ws_th.start()
            sleep(5)

        # message受信待機
        self.__wait_first_data()

    #---------------------------------------------------------------------------
    # 終了処理
    #---------------------------------------------------------------------------
    def __exit(self):
        self.ws.close()
        self.data['connection'] = False
        for i in self.data['timestamp']:
            self.data['timestamp'][i] = None

    #---------------------------------------------------------------------------
    # message受信待機
    #---------------------------------------------------------------------------
    def __wait_first_data(self):
        self.logger.info('Waiting for first data...')

        for i in self.data['timestamp']:
            # 実際に使う際はポジションは取得してからの方がいいです
            if i in ['position', 'execution', 'order']:
                continue
            while not self.data['timestamp'][i]:
                sleep(0.1)

        self.logger.info('Received first data.')

    #---------------------------------------------------------------------------
    # [WebSocket] on open
    #---------------------------------------------------------------------------
    def __on_open(self):
        self.logger.info('WebSocket opend.')

        if 'position' in self.channel_list or 'order' in self.channel_list or 'execution' in self.channel_list:
            # timestamp足してあげないとAuthエラーが出る
            timestamp = int((time() + 10.0) * 1000)
            param_str = 'GET/realtime' + str(timestamp)
            sign = hmac.new(self.secret.encode('utf-8'),
                            param_str.encode('utf-8'), hashlib.sha256).hexdigest()
            self.ws.send(json.dumps(
                        {'op': 'auth', 'args': [self.api_key, timestamp, sign]}))

            self.logger.info('Send auth.')

        self.ws.send(json.dumps(
                    {'op': 'subscribe', 'args': self.channel_list}))

        self.logger.info('Send subscribe.' + str(self.channel_list))

    #---------------------------------------------------------------------------
    # [WebSocket] on close
    #---------------------------------------------------------------------------
    def __on_close(self):
        self.logger.info('WebSocket Closed.')

    #---------------------------------------------------------------------------
    # [WebSocket] on error
    #---------------------------------------------------------------------------
    def __on_error(self, error):
        self.logger.error(f'WebSocket Error : {error}')
        self.__exit()
        self.__connect(self.endpoint)

    #---------------------------------------------------------------------------
    # [WebSocket] on message
    #---------------------------------------------------------------------------
    def __on_message(self, message):
        try:
            message = json.loads(message)
            topic = message.get('topic')
            data = message.get('data')
            ret_msg = message.get('ret_msg')
            self.data['timestamp'][topic] = time()

            # trade
            if topic == 'trade.' + self.symbol:
                for d in data:
                    self.data['last_price'] = d['price']
                    self.data['execution'].append(d)
                    self.callback_queue.put({'topic': 'trade', 'data': d})

            # instrument info
            elif topic == 'instrument_info.100ms.' + self.symbol:
                if message['type'] == 'snapshot':
                    self.data['instrument'] = data
                else:
                    if self.data['instrument'] and data['update']:
                        self.data['instrument'].update(data['update'][0])
                        if 'last_price_e4' in data['update'][0].keys():
                            self.callback_queue.put({'topic': 'instrument', 'data': self.data['instrument']})

            # orderbook
            elif topic == 'orderBook_200.100ms.' + self.symbol: # 'orderBookL2_25.'

                if message['type'] == 'snapshot':
                    self.board_snapshot_bids_dict.clear()
                    self.board_snapshot_asks_dict.clear()
                    for d in data:
                        if d['side'] == 'Buy':
                            self.board_snapshot_bids_dict[float(d['price'])] = [float(d['price']), float(d['size'])]
                        elif d['side'] == 'Sell':
                            self.board_snapshot_asks_dict[float(d['price'])] = [float(d['price']), float(d['size'])]

                else:
                    if data['delete']:
                        for d in data['delete']:
                            if d['side'] == 'Buy':
                                del self.board_snapshot_bids_dict[float(d['price'])]
                            elif d['side'] == 'Sell':
                                del self.board_snapshot_asks_dict[float(d['price'])]

                    if data['insert']:
                        for i in data['insert']:
                            if i['side'] == 'Buy':
                                self.board_snapshot_bids_dict[float(i['price'])] = [float(i['price']), float(i['size'])]
                            elif i['side'] == 'Sell':
                                self.board_snapshot_asks_dict[float(i['price'])] = [float(i['price']), float(i['size'])]

                    if data['update']:
                        for u in data['update']:
                            if u['side'] == 'Buy':
                                self.board_snapshot_bids_dict[float(u['price'])] = [float(u['price']), float(u['size'])]
                            elif u['side'] == 'Sell':
                                self.board_snapshot_asks_dict[float(u['price'])] = [float(u['price']), float(u['size'])]

                with self.__lock:
                    self.data['board_snapshot']['bids'] = [v for v in reversed(self.board_snapshot_bids_dict.values())]
                    self.data['board_snapshot']['asks'] = [v for v in self.board_snapshot_asks_dict.values()]

            # ohlcv
            elif topic == 'klineV2.' + self.period + '.' + self.symbol:
                d = data[0]
                ohlcv = [int(d['start']), float(d['open']), float(d['high']), float(d['low']), float(d['close']), int(d['volume'])]
                if len(self.last_ohlcv) > 0 and int(d['start']) > self.last_ohlcv[0]:
                    self.data['ohlcv'].append(self.last_ohlcv)
                    self.callback_queue.put({'topic': 'ohlcv', 'data': self.last_ohlcv})
                self.last_ohlcv = ohlcv

            # position
            elif topic == 'position':
                if data[0]['symbol'] == self.symbol:
                    pre_pos_size = -1
                    pre_balance = -1.0
                    if len(self.data['position']) > 0:
                        pre_pos_size = int(self.data['position']['size'])
                        pre_balance = float(self.data['position']['wallet_balance'])
                    self.data['position'] = data[0]
                    if ((pre_pos_size != int(self.data['position']['size'])) or
                        (pre_balance != float(self.data['position']['wallet_balance']))):
                        self.callback_queue.put({'topic': 'position', 'data': data[0]})

            # execution
            elif topic == 'execution':
                for d in data:
                    if d['symbol'] == self.symbol:
                        self.data['my_execution'].append(d)
                self.callback_queue.put({'topic': 'execution', 'data': data})

            # order
            elif topic == 'order':
                lst_delete_order = []
                for d in data:
                    if d['symbol'] == self.symbol:
                        self.data['my_order'].append(d)

                        is_delete = False
                        if 'order_status' in d.keys():
                            if d['order_status'] == 'Canceled' or d['order_status'] == 'Filled':
                                is_delete = True
                        if 'leaves_qty' in d.keys():
                            if d['leaves_qty'] <= 0:
                                is_delete = True
                        if is_delete:
                            if d['order_id'] in self.data['my_open_order'].keys():
                                self.data['my_open_order'].pop(d['order_id'])
                            lst_delete_order.append(d)
                        else:
                            self.data['my_open_order'][d['order_id']] = d

                self.callback_queue.put({'topic': 'order',
                                         'data': {
                                             'open': [o for o in self.data['my_open_order'].values()],
                                             'close': lst_delete_order,
                                         }})

            elif 'success' in message.keys():
                if message['success'] == True:
                    if ret_msg == 'pong':
                        pass
                    elif len(message['request']['args']) == len(self.channel_list):
                        self.data['connection'] = True
                else:
                    raise Exception(f'Connection failed: {message}')

            else:
                raise Exception(f'Unknown message: {message}')

        except Exception:
            self.logger.error(traceback.format_exc())

    #---------------------------------------------------------------------------
    # 定期ping送信
    #---------------------------------------------------------------------------
    def __send_ping(self):
        # 30~60秒ごとにピンポンした方が良いらしい
        while True:
            self.ws.send('{"op":"ping"}')
            sleep(30)

    #---------------------------------------------------------------------------
    # WebSocket再接続
    #---------------------------------------------------------------------------
    def reconnect(self):
        self.logger.info('Try reconnecting...')
        self.__exit()
        self.__connect(self.endpoint)

    #---------------------------------------------------------------------------
    # orderbook取得
    #---------------------------------------------------------------------------
    def get_orderbooks(self):
        with self.__lock:
            bids = copy.copy(self.data['board_snapshot']['bids'])
            asks = copy.copy(self.data['board_snapshot']['asks'])
        return {'bids':bids, 'asks':asks}

    #---------------------------------------------------------------------------
    # WebSocketの受信messageからコールバックを呼び出すhandler
    #---------------------------------------------------------------------------
    def __callback_event_handler(self):
        try:
            while True:
                if self.callback_queue.empty():
                    sleep(0.01)
                    continue

                data = self.callback_queue.get()
                if data is not None:
                    topic = data['topic']
                    if topic in self.callback.keys() and self.callback[topic] != None:
                        self.callback[topic](self, data['data'])

                self.callback_queue.task_done()

        except Exception:
            self.logger.error(traceback.format_exc())


#===============================================================================
# WebSocketに設定するコールバック関数
#  (状態通知用に受信データを文字列整形して出力)
#===============================================================================
#-------------------------------------------------------------------------------
# OHLCVデータ受信時に呼び出される関数
#-------------------------------------------------------------------------------
# [@param]
#     ws           BybitWSインスタンス
#     data         受信data
# [return]
#-------------------------------------------------------------------------------
def callback_ohlcv(ws:BybitWS, data:dict):
    now_time = datetime.now(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M:%S')
    msg = f'[ohlcv] {now_time}\n'
    if len(data) > 0:
        ltp = float(ws.data['instrument']['last_price_e4']) / 10000
        mrk = float(ws.data['instrument']['mark_price_e4']) / 10000
        idx = float(ws.data['instrument']['index_price_e4']) / 10000
        oi = int(ws.data['instrument']['open_interest'])
        msg += f'ltp:{ltp:.1f} mark:{mrk:.2f} index:{idx:.2f} oi:{oi:,}\n'
        msg += f'timestamp:{data[0]} open:{data[1]} high:{data[2]} low:{data[3]} close:{data[4]} voilume:{data[5]}'
        print(msg)

#-------------------------------------------------------------------------------
# 自注文データ受信時に呼び出される関数
#-------------------------------------------------------------------------------
# [@param]
#     ws           BybitWSインスタンス
#     data         受信data
# [return]
#-------------------------------------------------------------------------------
def callback_order(ws:BybitWS, data:dict):
    now_time = datetime.now(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M:%S')
    msg = f'[order] {now_time}\n'
    if len(data['open']) > 0:
        msg += f' [opened]\n'
        for o in data['open']:
            price = float(o['price'])
            qty = int(o['qty'])
            lvs = int(o['leaves_qty'])
            msg += f"  Type   : {o['order_type']} {o['side']}\n"
            msg += f"  Status : {o['order_status']}\n"
            msg += f"  Price  : {price:.1f}\n"
            msg += f"  Qty    : {qty-lvs} / {qty}\n"
            msg += f"  Time   : {o['timestamp']}\n"
            msg += f"  Option : {o['time_in_force']}\n"
            msg += '\n'

    if len(data['close']) > 0:
        msg += f' [closed]\n'
        for o in data['close']:
            price = float(o['price'])
            qty = int(o['qty'])
            lvs = int(o['leaves_qty'])
            msg += f"  Type   : {o['order_type']} {o['side']}\n"
            msg += f"  Status : {o['order_status']}\n"
            msg += f"  Price  : {price:.1f}\n"
            msg += f"  Qty    : {qty-lvs} / {qty}\n"
            msg += f"  Time   : {o['timestamp']}\n"
            msg += f"  Option : {o['time_in_force']}\n"
            msg += '\n'

    if DISCORD_NOTIFY:
        Notify.discord_notify('```' + msg + '```')
    print(msg)

#-------------------------------------------------------------------------------
# 自注文の約定データ受信時に呼び出される関数
#-------------------------------------------------------------------------------
# [@param]
#     ws           BybitWSインスタンス
#     data         受信data
# [return]
#-------------------------------------------------------------------------------
def callback_execution(ws:BybitWS, data:dict):
    now_time = datetime.now(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M:%S')
    msg = f'[execution] {now_time}\n'
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

    if DISCORD_NOTIFY:
        Notify.discord_notify('```' + msg + '```')
    print(msg)

#-------------------------------------------------------------------------------
# ポジションデータ受信時に呼び出される関数
#-------------------------------------------------------------------------------
# [@param]
#     ws           BybitWSインスタンス
#     data         受信data
# [return]
#-------------------------------------------------------------------------------
def callback_position(ws:BybitWS, data:dict):
    now_time = datetime.now(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M:%S')
    msg = f'[position] {now_time}\n'
    ltp = float(ws.data['last_price'])
    sz  = int(data['size'])
    ent = float(data['entry_price'])
    pnl = 0
    if sz > 0:
        pnl = ltp - ent if data['side'] == 'Buy' else ent - ltp
    liq = float(data['liq_price'])
    lvr = float(data['leverage'])
    pos = float(data['position_margin'])
    tp  = float(data['take_profit'])
    sl  = float(data['stop_loss'])
    ab  = float(data['available_balance'])
    wb  = float(data['wallet_balance'])
    msg += f"  Side       : {data['side']}\n"
    msg += f"  Size       : {sz} ({pos:.8f})\n"
    msg += f"  LastPrice  : {ltp:.1f}\n"
    msg += f"  Entry      : {ent:.2f} ({pnl:+.2f})\n"
    msg += f"  Liquid     : {liq:.2f}\n"
    msg += f"  TakeProfit : {tp:.2f}\n"
    msg += f"  StopLoss   : {sl:.2f}\n"
    msg += f"  Leverage   : {lvr:.2f}\n"
    msg += f"  Balance    : {wb:.8f} ({ab:.8f})\n"
    msg += '\n'

    if DISCORD_NOTIFY:
        Notify.discord_notify('```' + msg + '```')
    print(msg)


#===============================================================================
# main
#===============================================================================
if __name__ == '__main__':

    # Discord設定
    if DISCORD_NOTIFY:
        Notify.DISCORD_URL = DISCORD_WEBHOOK_URL

    # WebSocketチャンネル設定
    #   購読するチャンネルのリスト
    channel = [
        #'trade.' + BYBIT_SYMBOL,
        #'instrument_info.100ms.' + BYBIT_SYMBOL,
        #'orderBook_200.100ms.' + BYBIT_SYMBOL,
        #'klineV2.' + BYBIT_PERIOD + '.' + BYBIT_SYMBOL,
        #'position',
        #'execution',
        #'order',
    ]
    # 空リストの場合は上記defaultチャンネルを全て購読

    # WebSocketコールバック設定
    #   dict key   : topic,
    #        value : callback関数 (Noneはコールバックなし)
    callback = {
        'trade': None,
        'instrument': None,
        'ohlcv': callback_ohlcv if BYBIT_OHLCV_NOTIFY else None,
        'position': callback_position if BYBIT_POSITION_NOTIFY else None,
        'execution': callback_execution if BYBIT_EXECUTION_NOTIFY else None,
        'order': callback_order if BYBIT_ORDER_NOTIFY else None,
    }

    # Bybit WebSocketインスタンス生成
    bybit = BybitWS(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_IS_TESTNET, symbol=BYBIT_SYMBOL, channel=channel, callback=callback)

    # ５分毎に状態出力
    while True:
        #pprint(bybit.data)
        now_time = datetime.now(timezone('Asia/Tokyo')).strftime('%m/%d %H:%M:%S')
        ltp = bybit.data['last_price']
        ob = bybit.get_orderbooks()
        bid = ob['bids'][0][0]
        ask = ob['asks'][0][0]
        print(f'[{now_time}] ltp:{ltp:.1f}  bid:{bid:.1f}  ask:{ask:.1f}')

        if bybit.data['position']:
            pos_data = bybit.data['position']
            sd  = pos_data['side']
            sz  = int(pos_data['size'])
            ent = float(pos_data['entry_price'])
            pnl = 0
            if sz > 0:
                pnl = ltp - ent if sd == 'Buy' else ent - ltp
            liq = float(pos_data['liq_price'])
            pos = float(pos_data['position_margin'])
            wb  = float(pos_data['wallet_balance'])
            print(f'[{now_time}] {sd}:{sz}  entry:{ent:.2f}({pnl:+.1f})  liquid:{liq:.1f}  wallet:{wb:.8f}')

        sleep_time = 300 - int(time()) % 300
        sleep(sleep_time)

'''
【参考】

[Python]Bybit websocket
https://note.com/1one/n/n26615e8b5f76

bybit api document
https://bybit-exchange.github.io/docs/inverse/#t-websocket
'''
