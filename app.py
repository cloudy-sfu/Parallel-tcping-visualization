from flask import Flask, render_template, request, redirect, jsonify
from threading import Thread, Event
from itertools import count
import subprocess
from datetime import datetime, timedelta
import sqlite3
import re
import pytz
import socket
import time
import webbrowser


class TCPing(Thread):
    def __init__(self, hostname, db_path, table_name, max_ping=None):
        super(TCPing, self).__init__()
        self.db_path = db_path
        self.table_name = table_name
        self.stop_query = Event()
        self.hostname = hostname
        self.iterator = range(max_ping + 2) if max_ping else count()
        _hostname = hostname.split(':')
        self._hostname = _hostname[0]
        if len(_hostname) == 1:
            self._port = 80
        else:
            try:
                self._port = int(_hostname[-1])
            except ValueError:
                self._port = 80

    def run(self):
        """
        Reference https://github.com/zhengxiaowai/tcping
        :return:
        """
        for _ in self.iterator:
            if self.stop_query.is_set():
                break
            connection = sqlite3.connect(self.db_path)
            c = connection.cursor()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2)  # unit: seconds
                now_0 = datetime.now(tz=pytz.utc)
                s.connect((self._hostname, self._port))
                s.shutdown(socket.SHUT_RD)
                now_1 = datetime.now(tz=pytz.utc)
                s.close()
                delay = (now_1 - now_0) / timedelta(milliseconds=1)
                c.execute(f"INSERT INTO {self.table_name} (hostname, delay) VALUES (?, ?)",
                          (self.hostname, delay))
            except (socket.timeout, OSError):
                c.execute(f"INSERT INTO {self.table_name} (hostname, delay) VALUES (?, ?)",
                          (self.hostname, None))
            connection.commit()
            c.close()
            connection.close()
            time.sleep(1)


class Ping(Thread):
    """
    Deprecated. The source code is kept as a fallback in case TCPing has problems.
    """
    def __init__(self, hostname, db_path, table_name, max_ping=None):
        super(Ping, self).__init__()
        self.hostname = hostname
        self.iterator = range(max_ping + 2) if max_ping else count()
        self.db_path = db_path
        self.table_name = table_name
        self.stop_query = Event()

    def run(self):
        ping_process = subprocess.Popen(["ping", "-t", self.hostname], stdout=subprocess.PIPE)
        for _ in self.iterator:
            if self.stop_query.is_set():
                break
            delay_text = ping_process.stdout.readline().decode('utf-8').strip()
            if delay_text:
                connection = sqlite3.connect(self.db_path)
                c = connection.cursor()
                delay_number = re.search(r"time=(\d*)ms", delay_text)
                if delay_number:
                    delay_ms = int(delay_number.group(1))
                    c.execute(f"INSERT INTO {self.table_name} (hostname, delay) VALUES (?, ?)",
                              (self.hostname, delay_ms))
                elif delay_text == "Request timed out.":
                    c.execute(f"INSERT INTO {self.table_name} (hostname, delay) VALUES (?, ?)",
                              (self.hostname, None))
                connection.commit()
                c.close()
                connection.close()
            elif ping_process.poll() is not None:
                break


class PingController:
    def __init__(self, db_path, task_name):
        self.db_path = db_path
        self.threads = {}
        self.task_name = task_name
        connection = sqlite3.connect(self.db_path)
        c = connection.cursor()
        c.execute(f"CREATE TABLE IF NOT EXISTS {self.task_name} (t TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                  f"hostname text, delay float)")
        c.execute(f'CREATE INDEX IF NOT EXISTS t_idx ON {self.task_name} (t)')
        connection.commit()
        c.execute(f'SELECT DISTINCT hostname FROM {self.task_name}')
        for x in c.fetchall():
            self.threads[x[0]] = None
        c.close()
        connection.close()

    def start(self, hostname):
        if not hostname:
            return
        t = self.threads.get(hostname)
        if isinstance(t, TCPing) and t.is_alive():
            return
        t = TCPing(hostname, self.db_path, self.task_name)
        self.threads[hostname] = t
        t.start()

    def stop(self, hostname):
        if not hostname:
            return
        t = self.threads.get(hostname)
        if isinstance(t, TCPing) and t.is_alive():
            t.stop_query.set()
            t.join()
        self.threads[hostname] = None


app = Flask(__name__)
database_path = 'ping.db'
task = None
# Why, unlike cloudy-sfu/Homework-grader, do I use global variable "task", instead of recognizing the task by its name
# in "GET" arguments?
# Because "task" has threads. I expect only one pool is actively testing delays at the same time. If the user switch
# to another task, all threads in the previous task should be closed.


@app.route('/', methods=['GET'])
def index():
    connection = sqlite3.connect(database_path)
    c = connection.cursor()
    c.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    pools = [x[0] for x in c.fetchall()]
    return render_template('index.html', pools=pools)


@app.route('/task', methods=['GET'])
def view_task():
    global task
    if not isinstance(task, PingController):
        return '&leftarrow; Click the button to start with.'
    hosts = task.threads.keys()
    return render_template('task.html', hosts=hosts)


@app.route('/choose-task', methods=['POST'])
def choose_task():
    global task
    if isinstance(task, PingController):
        for host in task.threads.keys():
            task.stop(host)
        task = None
    new_task_name = request.form.get('table')
    if new_task_name:
        task = PingController(db_path=database_path, task_name=new_task_name)
    return redirect('/')


@app.route('/data', methods=['POST'])  # AJAX
def request_data():
    global task
    if not isinstance(task, PingController):
        return jsonify({'error': 'The data table isn\'t assigned, please choose the data table.'})
    hostname = request.form.get('host')
    if not hostname:
        return jsonify({'error': 'Hostname isn\'t defined.'})
    if hostname not in task.threads.keys():
        task.threads[hostname] = None
    connection = sqlite3.connect(task.db_path)
    c = connection.cursor()
    last_fetched = request.form.get('last_fetched')
    if last_fetched:  # TODO: database set datetime index
        c.execute(f"SELECT t, delay, iif(delay is null, 1, null) FROM {task.task_name} WHERE t > ? AND hostname = ?",
                  (last_fetched, hostname))
    else:
        c.execute(f"SELECT t, delay, iif(delay is null, 1, null) FROM {task.task_name} WHERE hostname = ?", (hostname,))
    records = list(zip(*c.fetchall()))
    c.close()
    if len(records) == 3:
        time_, delay, disconnected = records
        last_fetched = max(time_, key=lambda x_: datetime.strptime(x_, "%Y-%m-%d %H:%M:%S"))
    else:
        time_, delay, disconnected = [], [], []
    return jsonify(time=time_, delay=delay, disconnected=disconnected, last_fetched=last_fetched)


@app.route('/threads', methods=['POST'])  # AJAX
def control_threads():
    global task
    if not isinstance(task, PingController):
        return jsonify({'error': 'The data table isn\'t assigned, please choose the data table.'})
    host = request.form.get('host')
    action = request.form.get('action')
    match action:
        case 'start':
            if host:
                task.start(host)
            else:
                for host in task.threads.keys():
                    task.start(host)
        case 'stop':
            if host:
                task.stop(host)
            else:
                for host in task.threads.keys():
                    task.stop(host)
        case 'delete':
            if host:
                task.stop(host)
                del task.threads[host]
                connection = sqlite3.connect(task.db_path)
                c = connection.cursor()
                c.execute(f"DELETE FROM {task.task_name} WHERE hostname = ?", (host,))
                connection.commit()
                c.close()
        case _:
            pass
    return ''


if __name__ == '__main__':
    webbrowser.open_new_tab('http://localhost:5000')
    app.run()
