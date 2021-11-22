import argparse
from bs4 import BeautifulSoup
import googlemaps
import json
from os.path import exists
import pendulum
import re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.keys import Keys
from urllib.parse import urlparse, parse_qs
import yagmail
import yaml


def get_ttime_cache(cache_file):
    cache = {}
    if exists(cache_file):
        with open(cache_file) as input_file:
            results_by_date = json.load(input_file)
            for (day, results) in results_by_date.items():
                parks = {} 
                for (park, time) in results.items():
                    parks[park] = time
                cache[day] = parks
    return cache


class TravelTimer:
    def __init__(self, google_maps_client, cache_file, from_location, adjust_avg_mph, departure_time, max_travel_time):
        self.cache = get_ttime_cache(cache_file)
        cache_key = departure_time.strftime("%Y-%m-%d")
        if cache_key not in self.cache:
            self.cache[cache_key] = {}
        self.cur_cache = self.cache[cache_key]
        self.cache_file = cache_file
        self.google_maps_client = google_maps_client
        self.from_location = from_location
        self.adjust_avg_meters_per_sec = adjust_avg_mph * 1609.34 / 3600
        self.depart = departure_time
        self.max_travel_time = max_travel_time

    def save_cache(self):
        with open(self.cache_file, "w") as outfile:
            json.dump(self.cache, outfile, indent=2)

    def adjust_travel_time(self, meters, estimated_seconds):
        avg_speed = meters / estimated_seconds
        adjusted_speed = avg_speed + self.adjust_avg_meters_per_sec
        estimated_seconds = meters / adjusted_speed
        est_hours = int(estimated_seconds/3600)
        est_minutes = int((estimated_seconds-est_hours*3600)/60)
        miles = meters / 1609.34
        return est_hours, est_minutes, round(estimated_seconds), round(miles)

    def compute_estimate(self, to_location):
        if to_location not in self.cur_cache:
            directions = self.google_maps_client.directions(self.from_location, to_location, departure_time=self.depart)
            if directions is not None and len(directions) > 0:
                full_trip = directions[0]['legs'][0]
                meters = full_trip['distance']['value']
                if 'duration_in_traffic' in full_trip:
                    seconds = full_trip['duration_in_traffic']['value']
                else:
                    seconds = full_trip['duration']['value']
                est_time = self.adjust_travel_time(meters, seconds)
                self.cur_cache[to_location] = est_time

        return self.cur_cache[to_location]

    def allowed_time(self, est_time):
        if self.max_travel_time > 0:
            return est_time[0] < self.max_travel_time or (est_time[0] == self.max_travel_time and est_time[1] == 0)
        return True


def next_n_startdays(n, scan_from, day_of_week, days_to_add, timezone):
    startdays = []
    next_start = pendulum.parse(scan_from, tz=timezone).next(day_of_week)
    for _ in range(n):
        startdays.append("%i/%i/%i" % (next_start.month, next_start.day, next_start.year))
        next_start = next_start.add(days=days_to_add)
    return startdays


def do_search(driver, date, nights, resolved_address, interest, looking_for, occupants, rv_length):
    driver.get("https://texasstateparks.reserveamerica.com/unifSearch.do")

    if resolved_address is not None:
        driver.execute_script("UnifSearchEngine.selectResolvedAddress('" + resolved_address + "','0', 3)")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "interest")))

    if interest is not None:
        interest_selector = Select(driver.find_element_by_id('interest'))
        interest_selector.select_by_value(interest)

    if looking_for is not None:
        looking_for_selector = Select(driver.find_element_by_id('lookingFor'))
        looking_for_selector.select_by_value(str(looking_for))

    if occupants is not None:
        occupants_input = driver.find_element_by_id('camping_2001_3012')
        occupants_input.clear()
        occupants_input.send_keys(str(occupants))

    if rv_length is not None:
        length_input = driver.find_element_by_id('camping_2001_3013')
        length_input.clear()
        length_input.send_keys(str(rv_length))

    date_input = driver.find_element_by_id('campingDate')
    date_input.clear()
    date_input.send_keys(date)
    date_input.send_keys(Keys.ENTER)  # necessary to clear calendar popup

    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "lengthOfStay")))
    nights_input = driver.find_element_by_id('lengthOfStay')
    nights_input.clear()
    nights_input.send_keys(str(nights))

    submit = driver.find_element_by_css_selector("#btnDiv button")
    submit.click()


def collect_results(driver, host, travel_timer, only_parks, exclude_parks, seen_parks, site_includes, site_excludes,
                    sort_key, sort_reversed):
    results = []
    includes_pattern = re.compile('|'.join(site_includes))
    excludes_pattern = re.compile('|'.join(site_excludes))
    current_page = 0
    num_pages = 99
    only_park_ids = None
    if only_parks is not None:
        only_park_ids = [int(park_id) for park_id in only_parks.split(',')]
    exclude_park_ids = None
    if exclude_parks is not None:
        exclude_park_ids = [int(park_id) for park_id in exclude_parks.split(',')]
    results_page_url = driver.current_url + "?currentPage=%i"
    while current_page < num_pages:
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        if current_page == 0:
            pagination = soup.find(class_='usearch_results_control')
            page_options = pagination.findAll('option')
            num_pages = len(page_options)
        for card in soup.findAll(class_='facility_view_card'):
            link = card.find(class_='facility_link')
            avails = []
            book = card.find(class_='check_avail_panel')
            if book is not None and 'Book' in book.text:
                park_id = parse_qs(urlparse(link['href']).query)['parkId'][0]
                if only_park_ids is not None and int(park_id) not in only_park_ids:
                    continue
                if exclude_park_ids is not None and int(park_id) in exclude_park_ids:
                    continue
                est_time = travel_timer.compute_estimate(link.text)
                if not travel_timer.allowed_time(est_time):
                    continue
                for avail in card.findAll(class_='site_type_item_redesigned'):
                    avail_link = avail.find('a')
                    site_info = avail_link.text
                    if excludes_pattern.search(site_info) is None and includes_pattern.search(site_info) is not None:
                        avails.append(site_info)
                if len(avails) > 0:
                    results.append({
                        'park': link.text,
                        'seen': park_id in seen_parks,
                        'link': host + link['href'],
                        'id': park_id,
                        'miles': est_time[3],
                        'estimated_time_seconds': est_time[2],
                        'estimated_time': "%s hours %s minutes" % (est_time[0], est_time[1]),
                        'availability': avails
                    })
        current_page += 1
        if current_page < num_pages:
            driver.get(results_page_url % current_page)
    return sorted(results, key=lambda r: r[sort_key], reverse=sort_reversed)


def get_option(cfg, section, name, default=None):
    if name in cfg[section]:
        return cfg[section][name]
    return default


def get_prev_parks(cache_file):
    prev_parks = {}
    with open(cache_file) as input_file:
        results_by_date = json.load(input_file)
        for (day, results) in results_by_date:
            parks = set()
            for park_record in results:
                parks.add(park_record['id'])
            prev_parks[day] = parks
    return prev_parks


def run_searches(cfg, args):
    host = get_option(cfg, 'search', 'host')
    timezone = get_option(cfg, 'search', 'timezone')
    start_weekday = args.start_dow
    length_of_stay = args.num_days
    num_weeks = args.scan_weeks
    resolved_address = get_option(cfg, 'search', 'resolved_address')
    interest = get_option(cfg, 'search', 'interest')
    looking_for = get_option(cfg, 'search', 'looking_for')
    occupants = get_option(cfg, 'search', 'camping_occupants')
    rv_length = get_option(cfg, 'search', 'rv_length')
    site_includes = get_option(cfg, 'results', 'site_include')
    site_excludes = get_option(cfg, 'results', 'site_exclude')
    sort_key = get_option(cfg, 'results', 'sort_key')
    sort_reversed = get_option(cfg, 'results', 'sort_reversed')
    usual_departure_hour = get_option(cfg, 'results', 'usual_departure_hour')

    maps_client = googlemaps.Client(key=get_option(cfg, 'travel', 'google_api_key'))
    ttime_cache_file = get_option(cfg, 'travel', 'cache_file')
    from_location = get_option(cfg, 'travel', 'from')
    adjust_avg_mph = get_option(cfg, 'travel', 'adjust_avg_mph', default=0)
    first_departure = pendulum.parse(args.scan_from, tz=timezone).next(start_weekday).add(hours=usual_departure_hour)
    travel_timer = TravelTimer(maps_client, ttime_cache_file, from_location, adjust_avg_mph, first_departure, args.max_travel_time)

    chrome_options = Options()
    if cfg['selenium']['headless']:
        chrome_options.add_argument('--headless')
    driver = webdriver.Chrome(cfg['selenium']['chrome_driver'], options=chrome_options)

    prev_parks_by_date = {}
    if args.diff_only and args.cache_file is not None and exists(args.cache_file):
        prev_parks_by_date = get_prev_parks(args.cache_file)

    results = []
    for start_day in next_n_startdays(num_weeks, args.scan_from, start_weekday, 7, timezone):
        do_search(driver, start_day, length_of_stay, resolved_address, interest, looking_for, occupants, rv_length)
        if start_day in prev_parks_by_date:
            omit_parks = prev_parks_by_date[start_day]
        else:
            omit_parks = set()
        results.append((start_day, collect_results(
            driver, host, travel_timer, args.parks, args.exclude_parks, omit_parks, site_includes, site_excludes, sort_key, sort_reversed)))
    
    travel_timer.save_cache()

    if args.cache_file is not None:
        with open(args.cache_file, "w") as results_out:
            json.dump(results, results_out, indent=2)

    if args.diff_only:
        count = 0
        for i in range(len(results)):
            results[i] = (
                results[i][0],
                list(filter(lambda record: not record['seen'], results[i][1]))
            )
            count += len(results[i][1])
        if count == 0:
            args.send_email = False

    print(json.dumps(results, indent=2))

    if args.send_email:
        start_date = results[0][0]
        end_date = results[-1][0]
        if start_date == end_date:
            date_range = start_date
        else:
            date_range = f"{start_date} - {end_date}"
        if args.diff_only:
            email_body = cfg['email']['heading_diff'].format(length=length_of_stay, date=date_range)
        else:
            email_body = cfg['email']['heading'].format(length=length_of_stay, date=date_range)
        email_body += f"<h4>Estimated travel times from {from_location}</h4>"
        for r in results:
            email_body += f"<h3>{r[0]}</h3>"
            for avail in r[1]:
                div_template = "<div><a href='{link}'><b>{park}</b></a> {miles} miles, estimated {estimated_time}</div><ul>"
                email_body += div_template.format(**avail)
                for site_info in avail['availability']:
                    email_body += f"<li>{site_info}</li>"
                email_body += "</ul>"
        subject_vars = {
            'send_date': pendulum.now(timezone).to_date_string(),
            'date': date_range,
            'length': length_of_stay
        }
        if args.diff_only:
            email_subject = cfg['email']['subject_diff'].format(**subject_vars)
        else:
            email_subject = cfg['email']['subject'].format(**subject_vars)

        yag = yagmail.SMTP(config['email']['gmail_sender'])
        yag.send(
            to=config['email']['to'],
            subject=email_subject,
            contents=email_body
        )
        print("Email sent (%s) at %s" % (email_subject, pendulum.now(timezone).to_datetime_string()))

    driver.quit()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Scamp')
    parser.add_argument('--cfg', dest='cfg', action='store', default='config.yaml', help='config file')
    parser.add_argument('--no-email', dest='send_email', action='store_false',
                        help='do not send email (default send)')
    default_start = pendulum.now().add(days=-1).to_date_string()
    parser.add_argument('--scan-from', dest='scan_from', action='store', default=default_start,
                        help='date to start scan from (default yesterday: %s)' % default_start)
    parser.add_argument('--start-dow', dest='start_dow', action='store', type=int, default=5,
                        help='numeric day of week (Monday: 1) to look for availability (default Friday: 5)')
    parser.add_argument('--num-days', dest='num_days', action='store', type=int, default=2,
                        help='number of days of availability to look for (default: 2)')
    parser.add_argument('--scan-weeks', dest='scan_weeks', action='store', type=int, default=4,
                        help='number of weeks to scan (default: 4)')
    parser.add_argument('--max-ttime', dest='max_travel_time', action='store', type=int, default=-1,
                        help='max number of hours willing to travel')
    parser.add_argument('--parks', dest='parks', action='store', default=None,
                        help='comma-separated list of park IDs to include in results (exclusively)')
    parser.add_argument('--exclude-parks', dest='exclude_parks', action='store', default=None,
                        help='comma-separated list of park IDs to exclude from results (exclusively)')
    parser.add_argument('--cache-file', dest='cache_file', action='store', default=None,
                        help='Cache file to use/update for comparison')
    parser.add_argument('--diff-only', dest='diff_only', action='store_true',
                        help='Only show new parks since last run stored in cache file')
    args = parser.parse_args()
    print(args)
    with open(args.cfg, "r") as cfg_file:
        config = yaml.load(cfg_file, Loader=yaml.FullLoader)
        run_searches(config, args)
