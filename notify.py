# -*- coding: utf-8 -*-
import os
import logging
import logging.handlers
import requests
import traceback

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
