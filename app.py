#!/usr/bin/env python
from threading import Lock
from flask import Flask, render_template, session, request, \
    copy_current_request_context
from flask_socketio import SocketIO, emit, join_room, leave_room, \
    close_room, rooms, disconnect
from ratelimit import limits
import urllib.request
import json
from pymongo import MongoClient
from config import *

async_mode = None

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, async_mode=async_mode)
ws_server_thread = None
downloader_thread = None
client = None
thread_lock = Lock()

ONE_MINUTE = 60


def extract_data_of_interest(raw_dict):
    result = {}
    city_data = raw_dict['city']
    result['coord'] = tuple((city_data['coord']['lat'], city_data['coord']['lon']))
    result['country'] = city_data['country']
    result['name'] = city_data['name']
    result['weather'] = []
    ext = raw_dict['list']
    for i in range(len(ext)):
        d = ext[i]['weather'][0]
        if d['main'] in ['Rain', 'Snow', 'Extreme']:
            result['weather'].append(tuple((d['description'], ext[i]['dt_txt'])))
    return result


@limits(calls=config["RATE_LIMIT"], period=ONE_MINUTE)
def data_fetch(api_url):
    url = urllib.request.urlopen(api_url)
    output = url.read().decode('utf-8')
    raw_dict = json.loads(output)
    url.close()
    return extract_data_of_interest(raw_dict)


def download_thread():
    count = 0
    while True:
        count += 1
        for zip_code in config["MONITOR_ZIP_CODES"]:
            base_url = 'http://api.openweathermap.org/data/2.5/forecast'
            app_id = '2c746781db3f4ff65548af3c616ef225'
            api_endpoint = '{}?zip={},us&units=metric&appid={}'.format(base_url, zip_code, app_id)
            doc = data_fetch(api_endpoint)
            db = client.weather
            collection = db.forecast
            collection.insert_one(doc)
            socketio.sleep(config["DOWNLOAD_THREAD_SLEEP_TIME"])
        socketio.sleep(60)


def refresh_thread():
    count = 0
    while True:
        socketio.sleep(config["REFRESH_THREAD_SLEEP_TIME"])
        count += 1
        db = client.weather
        collection = db.forecast
        cursor = collection.find({})
        for doc in cursor:
            for w in doc['weather']:
                doc[w[0]] = [w[1]] if (w[0] not in doc) else (doc[w[0]] + [w[1]])
            del doc['weather']
            pack_msg = doc.copy()
            del pack_msg['_id']
            socketio.emit('add_marker',
                          {'data': pack_msg},
                          namespace='/test')
        if ((config["REFRESH_THREAD_SLEEP_TIME"] * count) % 300) == 0:
            socketio.emit('refresh_tiles',
                          {'data': 'New Map Tiles'},
                          namespace='/test')


@app.route('/')
def index():
    return render_template('index.html', async_mode=socketio.async_mode)


@socketio.on('disconnect_request', namespace='/test')
def disconnect_request():
    @copy_current_request_context
    def can_disconnect():
        disconnect()

    session['receive_count'] = session.get('receive_count', 0) + 1
    # for this emit we use a callback function
    # when the callback function is invoked we know that the message has been
    # received and it is safe to disconnect
    client.weather.forecast.delete_many({})
    client.close()
    emit('my_response',
         {'data': 'Disconnected!', 'count': session['receive_count']},
         callback=can_disconnect)


@socketio.on('connect', namespace='/test')
def new_client_connection():
    global ws_server_thread, downloader_thread, client
    client = MongoClient('mongodb://localhost:27017/')
    client.weather.forecast.delete_many({})
    with thread_lock:
        if ws_server_thread is None:
            ws_server_thread = socketio.start_background_task(refresh_thread)
        if downloader_thread is None:
            downloader_thread = socketio.start_background_task(download_thread)
    emit('my_response', {'data': 'New Map Info', 'count': 0})


@socketio.on('disconnect', namespace='/test')
def test_disconnect():
    print('Client disconnected', request.sid)


if __name__ == '__main__':
    socketio.run(app, debug=True)
