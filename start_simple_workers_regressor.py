import redis
import multiprocessing
import sys
sys.path.insert(0, './worker')
import argparse
import worker
import pandas as pd
import numpy as np
import time
import random
import networks
from start_simple_worker_regressor import start_worker

if __name__ == "__main__":

    server_host = "192.168.0.115"
    server = redis.Redis(server_host)
    n_workers = 24
    # instruments = ["EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD"]
    instruments = ["EUR_USD"]
    inst_i = 0

    n_workers_total = 0
    def start_process(n):
        global inst_i

        granularity = "M1"

        start = np.random.randint(1514764800, 1546300800)

        max_time = 1546300800

        instrument = instruments[inst_i]
        inst_i = (inst_i + 1) % len(instruments)

        process = multiprocessing.Process(target=start_worker, args=(instrument, granularity, server_host, start, max_time))
        process.start()

        print("starting worker {n}".format(n=n))
        return process

    processes = []
    times = []
    for i in range(n_workers):
        processes.append(start_process(i))
        times.append(time.time())
        # time.sleep(1)

    while True:
        for i, process in enumerate(processes):
            while process.is_alive() and time.time() - times[i] < 15:
                time.sleep(0.1)
            if process.is_alive():
                # doing process.terminate() will for whatever reason make it
                # hang. doing process.join(time) doesn't properly close the
                # process, so it uses all the ram and ends up crashing. so this
                # is my janky solution instead of figuring out wtf is going on
                try:
                    print("terminating worker {i}".format(i=i))
                    process.terminate()
                    process.join()
                    inst_i = (inst_i - 1) % len(instruments)
                except WindowsError as e:
                    print("error terminating worker {i}".format(i=i))
                    print(e)

            started = False
            while not started:
                # if server.llen("experience_dev") < 1:
                if server.llen("experience") < 10000:
                    processes[i] = start_process(i)
                    times[i] = time.time()
                    started = True
                    # time.sleep(1)
                else:
                    time.sleep(0.1)
