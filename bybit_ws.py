# -*- coding: utf-8 -*-
import os
import hmac
import hashlib
import json
import logging
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
from notify import Notify

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
        self.period = '1'
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


if __name__ == '__main__':

    # WebSocketチャンネル設定
    #   購読するチャンネルのリスト
    channel = [
        'trade.BTCUSD',
        'instrument_info.100ms.BTCUSD',
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
        'ohlcv': None,
        'position': None,
        'execution': None,
        'order': None,
    }

    # Bybit WebSocketインスタンス生成
    bybit = BybitWS('API_KEY', 'API_SECRET', is_testnet=False, symbol='BTCUSD', channel=channel, callback=callback)

    # ５分毎に価格出力
    while True:
        #pprint(bybit.data)
        now_time = datetime.now(timezone('Asia/Tokyo')).strftime('%m/%d %H:%M:%S')
        ltp = bybit.data['last_price']
        print(f'[{now_time}] ltp:{ltp:.1f}')
        sleep_time = 300 - int(time()) % 300
        sleep(sleep_time)
