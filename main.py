from bs4 import BeautifulSoup
import googlemaps
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
import sys
import yagmail
import yaml


class TravelTimer:
    def __init__(self, google_maps_client, from_location, adjust_avg_mph):
        self.cache = {}
        self.google_maps_client = google_maps_client
        self.from_location = from_location
        self.adjust_avg_meters_per_sec = adjust_avg_mph * 1609.34 / 3600

    def adjust_travel_time(self, meters, estimated_seconds):
        avg_speed = meters / estimated_seconds
        adjusted_speed = avg_speed + self.adjust_avg_meters_per_sec
        estimated_seconds = meters / adjusted_speed
        est_hours = int(estimated_seconds/3600)
        est_minutes = int((estimated_seconds-est_hours*3600)/60)
        miles = meters / 1609.34
        return est_hours, est_minutes, round(estimated_seconds), round(miles)

    def compute_estimate(self, to_location):
        if to_location not in self.cache:
            directions = self.google_maps_client.directions(self.from_location, to_location)
            if directions is not None and len(directions) > 0:
                full_trip = directions[0]['legs'][0]
                meters = full_trip['distance']['value']
                seconds = full_trip['duration']['value']
                est_time = self.adjust_travel_time(meters, seconds)
                self.cache[to_location] = est_time

        return self.cache[to_location]


def next_n_startdays(n, day_of_week, days_to_add, timezone):
    startdays = []
    next_start = pendulum.now(timezone).add(days=-1).next(day_of_week)
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


def collect_results(driver, host, travel_timer, site_includes, site_excludes, sort_key, sort_reversed):
    results = []
    includes_pattern = re.compile('|'.join(site_includes))
    excludes_pattern = re.compile('|'.join(site_excludes))
    current_page = 0
    num_pages = 99
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
                est_time = travel_timer.compute_estimate(link.text)
                for avail in card.findAll(class_='site_type_item_redesigned'):
                    avail_link = avail.find('a')
                    site_info = avail_link.text
                    if excludes_pattern.search(site_info) is None and includes_pattern.search(site_info) is not None:
                        avails.append(site_info)
                if len(avails) > 0:
                    results.append({
                        'park': link.text,
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


def run_searches(cfg):
    host = get_option(cfg, 'search', 'host')
    timezone = get_option(cfg, 'search', 'timezone')
    start_weekday = get_option(cfg, 'search', 'start_weekday')
    length_of_stay = get_option(cfg, 'search', 'length_of_stay')
    num_weeks = get_option(cfg, 'search', 'num_weeks')
    resolved_address = get_option(cfg, 'search', 'resolved_address')
    interest = get_option(cfg, 'search', 'interest')
    looking_for = get_option(cfg, 'search', 'looking_for')
    occupants = get_option(cfg, 'search', 'camping_occupants')
    rv_length = get_option(cfg, 'search', 'rv_length')
    site_includes = get_option(cfg, 'results', 'site_include')
    site_excludes = get_option(cfg, 'results', 'site_exclude')
    sort_key = get_option(cfg, 'results', 'sort_key')
    sort_reversed = get_option(cfg, 'results', 'sort_reversed')

    maps_client = googlemaps.Client(key=get_option(cfg, 'travel', 'google_api_key'))
    from_location = get_option(cfg, 'travel', 'from')
    adjust_avg_mph = get_option(cfg, 'travel', 'adjust_avg_mph', default=0)
    travel_timer = TravelTimer(maps_client, from_location, adjust_avg_mph)

    chrome_options = Options()
    if cfg['selenium']['headless']:
        chrome_options.add_argument('--headless')
    driver = webdriver.Chrome(cfg['selenium']['chrome_driver'], options=chrome_options)

    results = []
    for start_day in next_n_startdays(num_weeks, start_weekday, 7, timezone):
        do_search(driver, start_day, length_of_stay, resolved_address, interest, looking_for, occupants, rv_length)
        results.append((start_day, collect_results(
            driver, host, travel_timer, site_includes, site_excludes, sort_key, sort_reversed)))

    email_body = cfg['email']['heading'] % num_weeks
    for r in results:
        email_body += "<h3>%s</h3>" % r[0]
        email_body += "<h4>Estimated travel times from %s</h4>" % from_location
        for avail in r[1]:
            email_body += "<div><a href='%s'><b>%s</b></a> (%s miles, estimated %s)</div><ul>" % \
                          (avail['link'], avail['park'], avail['miles'], avail['estimated_time'])
            for site_info in avail['availability']:
                email_body += "<li>%s</li>" % site_info
            email_body += "</ul>"
    email_subject = cfg['email']['subject'] % (pendulum.now(timezone).to_date_string(), num_weeks)
    if cfg['email']['enabled']:
        yag = yagmail.SMTP("leejack@gmail.com")
        yag.send(
            to=["leejack@gmail.com", "alc2005@gmail.com"],
            subject=email_subject,
            contents=email_body
        )
        print("Email sent (%s) at %s" % (email_subject, pendulum.now(timezone).to_datetime_string()))
    else:
        print(email_body)
    driver.quit()


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    with open(sys.argv[1], "r") as cfg_file:
        config = yaml.load(cfg_file, Loader=yaml.FullLoader)
        run_searches(config)
