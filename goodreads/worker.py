from __future__ import unicode_literals, division, absolute_import, print_function

__license__   = 'GPL v3'
__copyright__ = '2011, Grant Drake'

import socket, re, datetime, json
from collections import OrderedDict
from threading import Thread
import requests

from lxml.html import tostring
from six import text_type as unicode

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.localization import canonicalize_lang
from calibre.utils.date import utcfromtimestamp
import calibre_plugins.goodreads.config as cfg

# Define your desired tags
DESIRED_TAGS = { "Art", "Biography", "Business", "Chick Lit", "Children's", "Christian",
                 "Classics", "Comics", "Contemporary", "Cookbooks", "Crime",
                 "Fantasy", "LGBT", "Erotica", "Graphic Novels", "Historical Fiction",
                 "History", "Horror", "Humor", "Manga", "Memoir", "Music", "Mystery",
                 "Nonfiction", "Paranormal", "Philosophy", "Poetry", "Psychology",
                 "Religion", "Romance", "Technology and Computers", "Science Fiction",
                 "Self Help", "Suspense", "Spirituality", "Sports", "Thriller", "Travel", "Young Adult" }

def clean_html(raw):
    from calibre.ebooks.chardet import xml_to_unicode
    from calibre.utils.cleantext import clean_ascii_chars
    return clean_ascii_chars(xml_to_unicode(raw, strip_encoding_pats=True,
                                resolve_entities=True, assume_utf8=True)[0])

def parse_html(raw):
    try:
        from html5_parser import parse
    except ImportError:
        # Old versions of calibre
        import html5lib
        return html5lib.parse(raw, treebuilder='lxml', namespaceHTMLElements=False)
    else:
        return parse(raw)


class Worker(Thread): # Get details

    '''
    Get book details from Goodreads book page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.goodreads_id = self.isbn = None

        lm = {
                'eng': ('English', 'Englisch'),
                'fra': ('French', 'Français'),
                'ita': ('Italian', 'Italiano'),
                'dut': ('Dutch',),
                'deu': ('German', 'Deutsch'),
                'spa': ('Spanish', 'Espa\xf1ol', 'Espaniol'),
                'jpn': ('Japanese', u'日本語'),
                'por': ('Portuguese', 'Português'),
                }
        self.lang_map = {}
        for code, names in lm.items():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        try:
            retry = True
            retryCount = 0
            while retry and retryCount <= 10:
                retryCount += 1
                self.log('Get details attempt #%d'%retryCount)
                retry = self.get_details()
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def get_details(self):
        try:
            self.log.info('Goodreads book url: %r'%self.url)
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                self.log.error('URL malformed: %r'%self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Goodreads timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r'%self.url
                self.log.exception(msg)
            return False

        #open('e:\\goodreads.html', 'wb').write(raw)
        raw_utf8 = raw.decode('utf-8', errors='replace')

        if '<title>404 - ' in raw_utf8:
            self.log.error('URL malformed: %r'%self.url)
            return False

        try:
            root = parse_html(raw_utf8)
        except:
            msg = 'Failed to parse goodreads details page: %r'%self.url
            self.log.exception(msg)
            return False

        try:
            # Look at the <title> attribute for page to make sure that we were actually returned
            # a details page for a book. If the user had specified an invalid ISBN, then the results
            # page will just do a textual search.
            title_node = root.xpath('//title')
            if title_node:
                page_title = title_node[0].text.strip()
                if page_title is None or page_title.find('search results for') != -1:
                    self.log.error('Failed to see search results in page title: %r'%self.url)
                    return
        except:
            msg = 'Failed to read goodreads page title: %r'%self.url
            self.log.exception(msg)
            return

        errmsg = root.xpath('//*[@id="errorMessage"]')
        if errmsg:
            msg = 'Failed to parse goodreads details page: %r'%self.url
            msg += tostring(errmsg, method='text', encoding='unicode').strip()
            self.log.error(msg)
            return False

        try:
            (book_json, series_json, contributors_list_json, work_json) = self.parse_book_json(root)
            if not book_json:
                self.log('No book_json found in this response, retrying for another response')
                return True
            self.parse_details(root, book_json, series_json, contributors_list_json, work_json)
        except:
            msg = 'Failed attempting to read book json meta tag from: %r'%self.url
            self.log.exception(msg)
            return False
        
    def parse_book_json(self, root):
        self.log.info('Trying to parse book json for 2022 web page format')
        script_node = root.xpath('//script[@id="__NEXT_DATA__"]')
        if not script_node:
            self.log.info('Page is legacy html format as NO Json found')
            return (None, None, None, None)
        try:
            self.log.info('Json script node found, page in 2022+ html format')
            book_props_json = json.loads(script_node[0].text)
            # script has a complicated hierarchy we need to traverse to get node we want
            apolloState = book_props_json["props"]["pageProps"]["apolloState"]
            # There can be random results where apolloState is empty, so have to fallback to parsing page
            if len(apolloState.keys()) == 0:
                self.log.info('Empty apolloState node, will scrape page instead')
                return (None, None, None, None)

            # now must iterate keys to find the one starting with Book, and array of all Contributors
            book_json = None
            series_json = None
            contributors_list_json = []
            work_json = None
            for key in apolloState.keys():
                if key.startswith("Book:"):
                    # There can be multiple book nodes. Want the most useful one.
                    if "title" in apolloState[key]:
                        book_json = apolloState[key]
                elif key.startswith("Series:") and series_json is None:
                    # There can be multiple series nodes. Want only the first one.
                    series_json = apolloState[key]
                elif key.startswith("Contributor:"):
                    # There can be multiple contributor nodes, add to list.
                    contributors_list_json.append(apolloState[key])
                elif key.startswith("Work:"):
                    # Should only be one Work node, just grab the Stats from within it.
                    work_json = apolloState[key]
             
            #self.log.info('Got book json: ', book_json)
            #self.log.info('Got contributors json: ', contributors_list_json)
            return (book_json, series_json, contributors_list_json, work_json)
        except:
            self.log.exception('Failed to parse book json: %r'%script_node[0].text)
            return (None, None, None, None)
    
    def parse_details(self, root, book_json, series_json, contributors_list_json, work_json):
        title = None
        authors = []
        try:
            goodreads_id = self.parse_goodreads_id(self.url)
        except:
            self.log.exception('Error parsing goodreads id for url: %r'%self.url)
            goodreads_id = None

        try:
            if book_json:
                title = self.parse_title(book_json)
        except:
            self.log.exception('Error parsing title for url: %r'%self.url)

        try:
            if (book_json):
                authors = self.parse_authors(book_json, contributors_list_json)
        except:
            self.log.exception('Error parsing authors for url: %r'%self.url)

        if not title or not authors or not goodreads_id:
            self.log.error('Could not parse all of title/authors/goodreads id from: %r'%self.url)
            self.log.error('Found Goodreads id: %r Title: %r Authors: %r'%(goodreads_id, title,
                authors))
            return

        mi = Metadata(title, authors)
        self.log.info('parse_details - goodreads_id: {0}, mi: {1}'.format(goodreads_id,mi))
        mi.set_identifier('goodreads', goodreads_id)
        self.goodreads_id = goodreads_id

        try:
            if book_json:
                (series, series_index) = self.parse_series(book_json, series_json)
            if series is not None:
                mi.series = series
                mi.series_index = series_index
        except:
            self.log.exception('Error parsing series for url: %r'%self.url)

        try:
            if book_json:
                isbn = self.parse_isbn(book_json)
            if isbn is not None:
                self.isbn = mi.isbn = isbn
        except:
            self.log.exception('Error parsing ISBN for url: %r'%self.url)

        try:
            get_asin = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_ASIN]
            if get_asin is not None:
                if book_json:
                    asin = self.parse_asin(book_json)
                if asin is not None:
                    mi.set_identifier('amazon', asin)
        except:
            self.log.exception('Error parsing ASIN for url: %r'%self.url)

        try:
            if work_json:
                mi.rating = self.parse_rating(work_json)
            get_rating = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_GET_RATING, False)
            if get_rating:
                mi.set_identifier('grrating', str(mi.rating))
        except:
            self.log.exception('Error parsing ratings for url: %r'%self.url)

        try:
            get_votes = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_GET_VOTES, False)
            if get_votes:
                if work_json:
                    votes = self.parse_rating_count(work_json)
                mi.set_identifier('grvotes', str(votes))
        except:
            self.log.exception('Error parsing ratings for url: %r'%self.url)

        try:
            if book_json:
                comments = self.parse_comments(book_json)
            self.log.info('parse_comments: ', comments)
            if comments:
                mi.comments = comments
        except:
            self.log.exception('Error parsing comments for url: %r'%self.url)

        try:
            if book_json:
                self.cover_url = self.parse_cover(book_json)
        except:
            self.log.exception('Error parsing cover for url: %r'%self.url)
        mi.has_cover = bool(self.cover_url)

        try:
            if book_json:
                tags = self.parse_tags(book_json)
            if tags is not None:
                mi.tags = tags
        except:
            self.log.exception('Error parsing tags for url: %r'%self.url)

        try:
            first_published = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_FIRST_PUBLISHED, True)
            if book_json:
                mi.publisher = self.parse_publisher(book_json)
                mi.pubdate = self.parse_publish_date(book_json, work_json, first_published)
        except:
            self.log.exception('Error parsing publisher and date for url: %r'%self.url)

        try:
            if book_json:
                lang = self.parse_language(book_json)
            if lang is not None:
                mi.language = lang
        except:
            self.log.exception('Error parsing language for url: %r'%self.url)

        mi.source_relevance = self.relevance

        if self.goodreads_id is not None:
            if self.isbn is not None:
                self.plugin.cache_isbn_to_identifier(self.isbn, self.goodreads_id)
            if self.cover_url is not None:
                self.plugin.cache_identifier_to_cover_url(self.goodreads_id,
                        self.cover_url)
        self.plugin.clean_downloaded_metadata(mi)

        self.result_queue.put(mi)

    def parse_goodreads_id(self, url):
        return re.search(r'/show/(\d+)', url).groups(0)[0]

    def parse_title(self, book_json):
        if "title" not in book_json:
            return None
        title = book_json["title"]
        self.log.info("parse_title: ", title)
        return title

    def parse_series(self, book_json, series_json):
        if series_json is None or "title" not in series_json:
            return (None, None)
        series_name = series_json["title"]

        if "bookSeries" not in book_json:
            return (None, None)
        # It is an array of series, but we will only use the first one
        for book_series in book_json["bookSeries"]:
            if "userPosition" not in book_series:
                return (None, None)
            try:
                series_index = float(book_series["userPosition"])
            except:
                self.log.error('Could not parse series index: ', book_series["userPosition"])
                return (None, None)

            self.log.info("parse_series: series_name='%s', series_index='%s'" % (series_name, series_index))
            return (series_name, series_index)

    def parse_authors(self, book_json, contributors_list_json):
        # 2022 version we use the json from script in page to retrieve the details
        # User either requests all contributing authors, or only the main authors (latter is the default)
        # We would do this by only including authors who have a role of "Author"
        # according to the primaryContriborEdge/secondaryContributorEdges sections of book json.
        get_all_authors = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_ALL_AUTHORS]
        authors = []
        author_contributor_ids = []
        # Start with the primary contributor
        if (book_json.get("primaryContributorEdge") is None):
            return authors
        primary = book_json["primaryContributorEdge"]
        role = primary["role"]
        if (role == "Author" or role == "Pseudonym" or get_all_authors):
            contributor = primary["node"]["__ref"]
            author_contributor_ids.append(contributor[12:]) # strip off "Contributor:"
        if (book_json.get("secondaryContributorEdges") is not None):
            for secondary in book_json["secondaryContributorEdges"]:
                role = secondary["role"]
                if (role == "Author" or get_all_authors):
                    contributor = secondary["node"]["__ref"]
                    author_contributor_ids.append(contributor[12:]) # strip off "Contributor:""

        if (not author_contributor_ids and not get_all_authors):
            # We could not find a role = "Author" for this book. That can happen with comics for instance.
            # In this circumstance just grab the primary contributor as author to use.
            contributor = primary["node"]["__ref"]
            author_contributor_ids.append(contributor[12:]) # strip off "Contributor:"
       
        for contributor_json in contributors_list_json:
            if (contributor_json.get("name") is None):
                continue
            id = contributor_json["id"]
            if (id in author_contributor_ids):
                author_name = contributor_json["name"]
                self.log.info('parse_authors - author=%s' % author_name)
                authors.append(author_name)
            

        return authors

    def parse_rating(self, work_json):
        if "stats" not in work_json:
            return None
        stats_json = work_json["stats"]        
        if "averageRating" not in stats_json:
            return None
        rating = float(stats_json["averageRating"])
        self.log.info("parse_rating: ", rating)
        return rating

    def parse_rating_count(self, work_json):
        if "stats" not in work_json:
            return None
        stats_json = work_json["stats"]        
        if "ratingsCount" not in stats_json:
            return None
        rating_count = int(stats_json["ratingsCount"])
        self.log.info("parse_rating_count: ", rating_count)
        return rating_count

    def parse_comments(self, book_json):
        if "description" not in book_json:
            return None
        description = book_json["description"]
        if not description:
            return None
        comments = sanitize_comments_html(description)
        return comments

    def parse_cover(self, book_json):
        if "imageUrl" in book_json:
            img_url = book_json["imageUrl"]
            if img_url:
                # Unfortunately Goodreads sometimes have broken links so we need to do
                # an additional request to see if the URL actually exists
                self.log.info('Visiting image url: %s'%img_url)
                info = self.browser.open_novisit(img_url, timeout=self.timeout).info()
                if int(info.get('Content-Length')) > 1000:
                    return img_url
                else:
                    self.log.warning('Broken image for url: %s'%img_url)
            else:
                self.log.warning('Empty imageUrl found in book json trying to retrieve cover URL')
        else:
            self.log.warning('No imageUrl found in book json to retrieve cover URL')

    def parse_isbn(self, book_json):
        isbn = None
        if "details" in book_json:
            details_json = book_json["details"]
            if "isbn13" in details_json:
                isbn = details_json["isbn13"]
            elif "isbn" in details_json:
                isbn = details_json["isbn"]
        self.log.info("parse_isbn: ", isbn)
        return isbn

    def parse_asin(self, book_json):
        asin = None
        if "details" in book_json:
            details_json = book_json["details"]
            if "asin" in details_json:
                asin = details_json["asin"]
        self.log.info("parse_asin: ", asin)
        return asin

    def parse_publisher(self, book_json):
        publisher = None
        if "publisher" in book_json["details"]:
            publisher = book_json["details"]["publisher"]
            self.log.info('parse_publisher: ', publisher)
        return publisher

    def parse_publish_date(self, book_json, work_json, first_published):
        pub_date = None
        # Publication time can be in multiple places. In book_json it will be the
        # edition publication date, whereas in work_json it seems to be the first published (if present).
        epoch_time = None
        # We will use the book publication entry as a fallback if look for first published and work_json value is null
        if "publicationTime" in book_json["details"]:
            epoch_time = book_json["details"]["publicationTime"]
        if first_published and "publicationTime" in work_json["details"] and work_json["details"]["publicationTime"]:
            epoch_time = work_json["details"]["publicationTime"]
        if epoch_time:
            # Their time seems to have an extra 3 trailing zeroes which causes conversion grief!
            try:
                epoch_time = int(epoch_time) // 1000
                #self.log.info('Unix epoch publication time: ', epoch_time)
                pub_date = utcfromtimestamp(epoch_time)
                if first_published:
                    self.log.info('parse_publish_date: %s (First published)'%pub_date)
                else:
                    self.log.info('parse_publish_date: %s (Edition)'%pub_date)
            except:
                self.log.error('Failed to convert unix pub date to datetime of: ', epoch_time)
        return pub_date

    def parse_tags(self, book_json):
        # Goodreads does not have "tags", but it does have Genres (wrapper around popular shelves)
        # We will use those as tags (with a bit of massaging)
        # In 2022 version there is no hierarchy of genres, so just a flat list of tags really.
        if "bookGenres" not in book_json:
            return []
        genre_tags = list()
        for book_genre_json in book_json["bookGenres"]:
            if "genre" in book_genre_json:
                genre_name = book_genre_json["genre"]["name"]
                genre_tags.append(genre_name)
        calibre_tags = self._convert_genres_to_calibre_tags(genre_tags)
        self.log.info("parse_tags: %s"%','.join(calibre_tags))
        if len(calibre_tags) > 0:
            return calibre_tags




   def parse_tags(self, book_json):
       """
       Parses tags from Goodreads genres, and if necessary, calls the LLM to categorize the book.

       Args:
          book_json (dict): The JSON data for the book, containing genres and other metadata.

       Returns:
           list: A list of validated tags, potentially including those added by the LLM.
       """
       # Goodreads does not have "tags", but it does have Genres (wrapper around popular shelves)
       # We will use those as tags (with a bit of massaging)
       # In 2022 version there is no hierarchy of genres, so just a flat list of tags really.
       if "bookGenres" not in book_json:
           return []
    
       genre_tags = []
       for book_genre_json in book_json["bookGenres"]:
           if "genre" in book_genre_json:
               genre_name = book_genre_json["genre"]["name"]
               genre_tags.append(genre_name)
    
       # Convert Goodreads genres to Calibre-compatible tags
       calibre_tags = self._convert_genres_to_calibre_tags(genre_tags)
       self.log.info("parse_tags: %s" % ','.join(calibre_tags))
    
       # Check if any of the tags match DESIRED_TAGS, case-insensitively
       matching_tags = {tag for tag in calibre_tags if tag.lower() in map(str.lower, DESIRED_TAGS)}
       if matching_tags:
          return list(calibre_tags)  # Return list including existing desired tags
    
       # If no desired tags found, make an LLM call for categorization
       llm_tags = self.filter_llm_generated_tags(book_json)
       return llm_tags + calibre_tags

    def filter_llm_generated_tags(self, book_json):
       """
       Calls the local LLM (e.g., Ollama, LM Studio) to categorize the book and filters response for desired tags.

       Args:
           book_json (dict): Book information including title and description.

       Returns:
           list: A list of tags from DESIRED_TAGS that appear in the LLM response.
       """
       # Construct the prompt
       prompt = f"Classify the following book described, replying with ONLY one or more of these tags: {', '.join(DESIRED_TAGS)}.\n\n"
       prompt += f"Title: {book_json['title']}\n"
       if 'description' in book_json:
           prompt += f"Description: {book_json['description']}\n"

       # LLM API call setup
       llm_url = "http://192.168.1.203:11435/endpoint"  # Replace PORT with the correct port
       headers = {"Content-Type": "application/json"}
       payload = {
           "prompt": prompt,
           "model": "mistral"  # Replace with the model name or ID specific to Ollama or LM Studio
       }
    
       # Make the API call and parse response
       try:
          response = requests.post(llm_url, json=payload, headers=headers)
          response.raise_for_status()  # Raise an exception for HTTP errors
          llm_response = response.json().get("text", "").strip()
        
          # Filter out only the desired tags from the LLM response
          matched_tags = self.match_desired_tags(llm_response, DESIRED_TAGS)
        
          # Return list of matched tags, or empty list if none found
          return matched_tags
    
        except requests.RequestException as e:
            self.log.info(f"Error contacting LLM: {e}")
            return []  # Fallback to an empty list if the LLM call fails

    def match_desired_tags(self, response_text, desired_tags):
        """
        Matches any tags in desired_tags that are found in the response_text.
    
        Args:
           response_text (str): The text response from the LLM.
           desired_tags (set): The set of tags we're interested in.

        Returns:
           list: A list of matched tags from desired_tags.
       """
       # Normalize and split response text for easier matching
       response_tags = re.split(r'[,\n]', response_text.lower())
       response_tags = [tag.strip().capitalize() for tag in response_tags]
    
       # Filter for tags in the desired set
       return [tag for tag in response_tags if tag in desired_tags]

    def _convert_genres_to_calibre_tags(self, genre_tags):
        map_genres = cfg.plugin_prefs[cfg.STORE_NAME].get(cfg.KEY_MAP_GENRES, cfg.DEFAULT_STORE_VALUES[cfg.KEY_MAP_GENRES])
        if not map_genres:
            # User has disabled Goodreads tag filtering/mapping - all genres become tags
            return genre_tags
        # for each tag, add if we have a dictionary lookup
        calibre_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GENRE_MAPPINGS]
        calibre_tag_map = dict((k.lower(),v) for (k,v) in calibre_tag_lookup.items())
        tags_to_add = list()
        for genre_tag in genre_tags:
            tags = calibre_tag_map.get(genre_tag.lower(), None)
            if tags:
                for tag in tags:
                    if tag not in tags_to_add:
                        tags_to_add.append(tag)
        return list(tags_to_add)

    def _convert_date_text(self, date_text):
        # Note that the date text could be "2003", "December 2003" or "December 10th 2003"
        year = int(date_text[-4:])
        month = 1
        day = 1
        if len(date_text) > 4:
            text_parts = date_text[:len(date_text)-5].partition(' ')
            month_name = text_parts[0]
            # Need to convert the month name into a numeric value
            # For now I am "assuming" the Goodreads website only displays in English
            # If it doesn't will just fallback to assuming January
            month_dict = {"January":1, "February":2, "March":3, "April":4, "May":5, "June":6,
                "July":7, "August":8, "September":9, "October":10, "November":11, "December":12}
            month = month_dict.get(month_name, 1)
            if len(text_parts[2]) > 0:
                day = int(re.match('([0-9]+)', text_parts[2]).groups(0)[0])
        from calibre.utils.date import utc_tz
        return datetime.datetime(year, month, day, tzinfo=utc_tz)

    def parse_language(self, book_json):
        if "language" in book_json["details"]:
            lang_name = book_json["details"]["language"]["name"]
            ans = self.lang_map.get(lang_name, None)
            if ans:
                self.log.info('parse_language: ', ans)
                return ans
            ans = canonicalize_lang(lang_name)
            if ans:
                self.log.info('parse_language: ', ans)
                return ans
