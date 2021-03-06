import argparse
from collections import Counter
import logging
from logging import handlers
import json
import math
import signal
import sys
import time
import urllib3
import enlighten

from requestmanager import RequestManager

arg_parser = argparse.ArgumentParser(description="Rate unlimiter")
arg_parser.add_argument("url")
arg_parser.add_argument("-t", "--threads", dest="threads", type=int, default=1)
arg_parser.add_argument("--timeout", dest="timeout", type=int, default=20)
arg_parser.add_argument("--method", dest="method", default="GET")
arg_parser.add_argument("--cooldown", dest="cooldown", type=int, default=10)
arg_parser.add_argument("--goal", dest="goal", type=int, default=5)
arg_parser.add_argument("--proxy-host", dest="proxy_host", default=None)
arg_parser.add_argument("--proxy-port", dest="proxy_port", type=int, default=8080)
arg_parser.add_argument("--debug", dest="debug", action="store_true")

args = arg_parser.parse_args()


def init_logging(debug=False):
    logger = logging.getLogger("rateunlimiter")
    logger.setLevel(logging.DEBUG)
    logformat = "%(asctime)s %(name)s %(levelname)s:%(message)s"
    log_formatter = logging.Formatter(fmt=logformat, datefmt='%Y-%m-%d %H:%M:%S')
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(log_formatter)
    stderr_handler.setLevel(logging.INFO)
    logger.addHandler(stderr_handler)
    if debug:
        file_handler = handlers.RotatingFileHandler("debug.log", maxBytes=2*1024*1024, backupCount=1, encoding="utf-8")
        file_handler.setFormatter(log_formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    return logger


def sig_handler(signum, frame):  # pylint: disable=unused-argument
    logger.info("Exiting...")
    if args.debug:
        debug_output = {}
        debug_output['request_times'] = request_times
        debug_output['fail_times'] = fail_times
        with open("debug_requests.json", "w") as f:
            json.dump(debug_output, f)
    sys.exit()


def process_decay(delay, min_delay=1):
    new_delay = 60/((60/delay)+1)
    if new_delay < min_delay:
        return min_delay
    else:
        return new_delay


def sleep_update(duration):
    while duration > 0:
        status_rate.update()
        if duration >= 1.00:
            time.sleep(1)
        else:
            time.sleep(duration)
        duration -= 1


def perform_requests(delay=0):
    global success_times
    min_delay = 0.5
    success_rate = 0
    max_rate = float("inf")
    first_fail = 0
    fail_count = 0
    success_count = 1
    rate_guesses = {}
    logger.info(f"Sleeping for {delay:.2f} seconds...")
    sleep_update(delay)
    while True:
        blocked = False
        c["total"] += 1
        if args.proxy_host:
            req = manager.request("GET", "http://ipinfo.io/ip")
            logger.info(f"Source IP: {req.data}")
        logger.debug(f"Performing request {c['total']}")
        try:
            req = manager.request("GET", f"{args.url}?{c['total']}")
            request_times.append(time.monotonic())
            logger.info(f"Received HTTP {req.status} response from server")
        except urllib3.exceptions.ProtocolError:
            blocked = True
        if req.status == 429 or req.status == 403:
            blocked = True
        req_rate = len(request_times) / (request_times[-1] - request_times[0]) * 60
        status_rate.update(cur_rate=f"{req_rate:.2f}")
        logger.debug(f"Current request rate: {req_rate:.2f} req/min")
        if blocked:
            success_count = 0
            if fail_count == 0:  # First fail, set new limits
                success_times = []
                max_rate = math.ceil(req_rate)
                min_delay = 60/max_rate
                first_fail = time.monotonic()
            fail_count += 1
            fail_times.append([time.monotonic(), fail_count, 1])
            elapsed_time = (fail_times[-1][0] - request_times[0])
            elapsed_min = math.floor(elapsed_time / 60)
            if not rate_guesses.get(elapsed_min):
                rate_guesses[elapsed_min] = round(len(request_times) / (request_times[-1] - request_times[0]) * 60 * elapsed_min)
                logger.debug(f"New guess: {rate_guesses[elapsed_min]} req/{elapsed_min} min")
            guess_str = ""
            guess_last = 0
            guess_rm = []
            for guess_interval, guess_count in rate_guesses.items():
                if guess_count == rate_guesses.get(guess_last, 0):
                    guess_rm.append(guess_last)
                guess_last = guess_interval
            for rm in guess_rm:
                rate_guesses.pop(rm)
            for guess_interval, guess_count in rate_guesses.items():
                guess_str += f" {guess_count} r/{guess_interval} min"
            status_guess.update(guess=guess_str)
            delay = 60*((args.goal/10)**fail_count)
        else:
            c["success"] += 1
            success_count += 1
            success_times.append([time.monotonic(), success_count, 1])
            if len(success_times) > 1:
                success_rate = len(success_times) / (success_times[-1][0] - success_times[0][0]) * 60
                # logger.info(f"Current success rate: {success_rate:.2f} r/min")
            if fail_count > 0:  # Block expired, calculate previous penalty
                penalty_guess = time.monotonic() - first_fail
                fail_count = 0
                delay = (cooldown_duration[::-1])[min(len(cooldown_duration)-1, fail_count)]
                # logger.info(f"Block expired, current penalty duration guess: {penalty_guess:.0f} seconds")
            delay = process_decay(delay, min_delay)
        logger.info(f"Sleeping for {delay:.2f} seconds...")
        sleep_update(delay)


if __name__ == "__main__":
    console_manager = enlighten.get_manager()
    status_rate = console_manager.status_bar(status_format="Rate Unlimiter{fill}Current rate: {cur_rate} req/min{fill}{elapsed}",
                                             color="bright_white_on_lightslategray",
                                             justify=enlighten.Justify.CENTER,
                                             cur_rate="-")
    status_guess = console_manager.status_bar(status_format="Current guess:{guess}",
                                              guess="",
                                              justify=enlighten.Justify.LEFT)
    logger = init_logging(args.debug)
    logger.info("Initializing...")
    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)
    logger.debug("Initializing connection pool...")
    INITIAL_DELAY = 15
    c = Counter()
    request_times = []
    success_times = []
    fail_times = []
    cooldown_duration = list(range(args.cooldown, 1, -2))
    manager = RequestManager(proxy_host=args.proxy_host, proxy_port=args.proxy_port, num_pools=1, maxsize=args.threads)
    c["total"] += 1
    if args.proxy_host:
        req = manager.request("GET", "http://ipinfo.io/ip")
        logger.info(f"Source IP: {req.data.decode()}")
    logger.debug(f"Performing request {c['total']}")
    req = manager.request("GET", args.url)
    request_times.append(time.monotonic())
    if req.status == 429:
        raise RuntimeError("Already rate-limited")
    if req.status == 405:
        raise RuntimeError("Invalid method: Server returned HTTP 405")
    c["success"] += 1
    success_times.append([time.monotonic(), 1, 1])
    logger.info(f"Received HTTP {req.status} response from server")
    perform_requests(INITIAL_DELAY)
