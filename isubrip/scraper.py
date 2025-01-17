import json
import re
from datetime import datetime, timedelta
from typing import Iterator, List, Union
from urllib.error import HTTPError

import m3u8
import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from m3u8.model import M3U8

from isubrip.enums import DataSource, SubtitlesType
from isubrip.constants import APPLETV_API_BASE_PARAMS, APPLETV_MOVIE_API_URL, APPLETV_STOREFRONTS_PATH, APPLETV_URL_REGEX, ITUNES_URL_REGEX
from isubrip.namedtuples import MovieData, PlaylistData, SubtitlesData
from isubrip.exceptions import InvalidURL, PageLoadError


class Scraper:
    """A class for scraping and downloading subtitles off of iTunes movie pages."""

    _atv_storefronts = None


    @staticmethod
    def get_movie_data(url: str, headers: Union[dict, None] = None) -> MovieData:
        """
        Scrape an iTunes / AppleTV page to find movie info and it's M3U8 playlist.

        Args:
            url (str): An iTunes store movie URL.
            headers (dict | None, optional): Headers to use for HTTP requests.
        
        Raises:
            InvalidURL: `itunes_url` is not a valid iTunes store movie URL.
            PageLoadError: HTML page did not load properly.
            HTTPError: HTTP request failed.

        Returns:
            MovieData: A MovieData (NamedTuple) object with movie's name, and an M3U8 object of the playlist
            if the playlist is found. None otherwise.
        """
        itunes_regex = re.fullmatch(ITUNES_URL_REGEX, url)
        appletv_regex = re.fullmatch(APPLETV_URL_REGEX, url)

        # Check whether URL is for iTunes or AppleTV
        if itunes_regex is not None:
            url = itunes_regex.group(1)
            request = requests.get(url, headers=headers)
            request.raise_for_status()

            # Response is JSON formatted
            if "application/json" in request.headers['content-type']:
                try:
                    json_data = json.loads(request.content)

                except json.JSONDecodeError:
                    raise PageLoadError("Recieved an invalid JSON response.")

                return Scraper._find_playlist_data_itunes_json_(json_data)

            # Response is HTML formatted
            elif "text/html" in request.headers['content-type'] and request.status_code != 404:
                html_data = BeautifulSoup(request.content, "lxml")
                return Scraper._find_playlist_data_itunes_html_(html_data)

            # Response is neither JSON nor HTML formatted (if the URL is not found, iTunes returns an XML response),
            # or an HTML 404 error was received.
            else:
                raise PageLoadError("Recieved an invalid response. Pleas assure the URL is valid.")

        elif appletv_regex is not None:
            if not Scraper._atv_storefronts:
                with open(APPLETV_STOREFRONTS_PATH, "r") as storefronts_file:
                    Scraper._atv_storefronts = json.load(storefronts_file)

            request_params = APPLETV_API_BASE_PARAMS

            # Add storefront ID to params
            request_params["sf"] = Scraper._atv_storefronts[appletv_regex.group(2).upper()]

            request = requests.get(APPLETV_MOVIE_API_URL + appletv_regex.group(3), headers=headers, params=request_params)
            request.raise_for_status()
            json_data = request.json()

            return Scraper._find_playlist_data_appletv_json_(json_data)

        else:
            raise InvalidURL(f"{url} is not a valid iTunes/AppleTV movie URL.")

    @staticmethod
    def _find_playlist_data_itunes_json_(json_data: dict) -> MovieData:
        """
        Scrape an iTunes JSON response to get movie info.

        Args:
            json_data (dict): A dictionary with iTunes data loaded from a JSON response.

        Returns:
            MovieData: A MovieData (NamedTuple) object with movie's name, and an M3U8 object of the playlist
            if the playlist is found. None otherwise.
        """
        itunes_id = json_data["pageData"]["id"]
        movie_data = json_data["storePlatformData"]["product-dv"]["results"][itunes_id]

        movie_title = movie_data["nameRaw"]
        movie_release_year = datetime.strptime(movie_data["releaseDate"], '%Y-%m-%d').year

        # Loop safely to find a matching playlist
        for offer in movie_data["offers"]:
            if isinstance(offer.get("type"), str) and offer["type"] in ["buy", "rent"]:
                if isinstance(offer.get("assets"), list) and len(offer["assets"]) > 0:
                    for asset in offer["assets"]:
                        playlist_url: str = asset["hlsUrl"]

                        # Assure playlist is valid
                        try:
                            m3u8.load(playlist_url)

                        # If m3u8 playlist is invalid, skip it
                        except ValueError:
                            continue

                        except HTTPError:
                            continue

                        return MovieData(DataSource.ITUNES, movie_title, movie_release_year, [PlaylistData(itunes_id, playlist_url)])

        return MovieData(DataSource.ITUNES, movie_title, movie_release_year, [])

    @staticmethod
    def _find_playlist_data_itunes_html_(html_data: BeautifulSoup) -> MovieData:
        """
        Scrape an iTunes HTML page to get movie info.

        Args:
            html_data (BeautifulSoup): A BeautifulSoup object of the page.

        Raises:
            PageLoadError: HTML page did not load properly.

        Returns:
            MovieData: A MovieData (NamedTuple) object with movie's name, and an M3U8 object of the playlist
            if the playlist is found. None otherwise.
        """
        # NOTE: This function is less reliable than `_find_playlist_data_itunes_json_`.

        itunes_id_tag: Union[Tag, NavigableString, None] = html_data.find("meta", attrs={"name": "apple:content_id"})
        if not isinstance(itunes_id_tag, Tag):
            raise PageLoadError("HTML page did not load properly.")

        itunes_id: str = itunes_id_tag.attrs["content"]

        # Scrape a dictionary on the webpage that has playlists data
        shoebox_data_tag: Union[Tag, NavigableString, None] = html_data.find("script", attrs={"id": "shoebox-ember-data-store", "type": "fastboot/shoebox"})

        # fastboot/shoebox data could not be found
        if not isinstance(shoebox_data_tag, Tag):
            raise PageLoadError("fastboot/shoebox data could not be found.")

        # Convert to dictionary structure
        shoebox_data: dict = json.loads(str(shoebox_data_tag.contents[0]).strip())

        # Loop safely to find a matching playlist
        if isinstance(shoebox_data[itunes_id].get("included"), list):
            movie_data: dict = shoebox_data[itunes_id]
            movie_title: str = movie_data["data"]["attributes"]["name"]
            movie_release_year = datetime.strptime(movie_data["data"]["attributes"]["releaseDate"], '%Y-%m-%d').year

            for item in movie_data["included"]:
                if isinstance(item.get("type"), str) and item["type"] == "offer":
                    if isinstance(item.get("attributes"), dict) and \
                        isinstance(item["attributes"].get("assets"), list) and \
                            len(item["attributes"]["assets"]) > 0:

                        for asset in item["attributes"]["assets"]:
                            if isinstance(asset, dict) and isinstance(asset.get("hlsUrl"), str):
                                playlist_url: str = item["attributes"]["assets"][0]["hlsUrl"]

                                # Try loading the playlist to assure it's working
                                try:
                                    m3u8.load(playlist_url)

                                # If m3u8 playlist is invalid, skip it
                                except (ValueError, HTTPError):
                                    continue

                                return MovieData(DataSource.ITUNES, movie_title, movie_release_year, [PlaylistData(itunes_id, playlist_url)])
        else:
            raise PageLoadError("Invalid shoebox data.")

        return MovieData(DataSource.ITUNES, movie_title, movie_release_year, [])

    @staticmethod
    def _find_playlist_data_appletv_json_(json_data: dict) -> MovieData:
        """
        Scrape an iTunes JSON response to get movie info.

        Args:
            json_data (dict): A dictionary with AppleTV data loaded from a JSON response.

        Returns:
            MovieData: A MovieData (NamedTuple) object with movie's name, and an M3U8 object of the playlist
            if the playlist is found. None otherwise.
        """
        # Scrape a dictionary on the webpage that has playlists data

        movie_title = json_data["data"]["content"]["title"]
        release_epoch = json_data["data"]["content"]["releaseDate"] // 1000

        # Release date epoch is not negative (After 01/01/1970)
        if release_epoch > 0:
            movie_release_year = datetime.fromtimestamp(release_epoch).year

        else:
            movie_release_year = (datetime(1970, 1, 1) + timedelta(seconds=release_epoch)).year

        playables_data = json_data["data"]["playables"]
        playlists: List[PlaylistData] = []
        itunes_ids_set = set()

        for playable in playables_data.values():
            if playable["isItunes"]:
                itunes_id = playable["externalId"]

                # Assure playlist on current offer isn't the same as another
                if itunes_id not in itunes_ids_set:
                    for offer in playable["itunesMediaApiData"]["offers"]:
                        playlist_url: str = offer["hlsUrl"]

                        # Try loading the playlist to assure it's working
                        try:
                            m3u8.load(playlist_url)

                        # If m3u8 playlist is invalid, skip it
                        except (ValueError, HTTPError):
                            continue

                        playlists.append(PlaylistData(itunes_id, playlist_url))
                        break

        return MovieData(DataSource.APPLETV, movie_title, movie_release_year, playlists)

    @staticmethod
    def _find_playlist_data_appletv_html_(html_data: BeautifulSoup) -> MovieData:
        """
        Scrape an AppleTV HTML page to find movie info and it's M3U8 playlist.

        Args:
            html_data (BeautifulSoup): A BeautifulSoup object of the page.

        Raises:
            PageLoadError: HTML page did not load properly.

        Returns:
            MovieData: A MovieData (NamedTuple) object with movie's name, and an M3U8 object of the playlist
            if the playlist is found. None otherwise.
        """
        # Scrape a dictionary on the webpage that has playlists data
        shoebox_data_tag: Union[Tag, NavigableString, None] = html_data.find("script", attrs={"id": "shoebox-uts-api", "type": "fastboot/shoebox"})

        # fastboot/shoebox data could not be found
        if not isinstance(shoebox_data_tag, Tag):
            raise PageLoadError("fastboot/shoebox data could not be found.")

        try:
            # Convert to dictionary structure
            shoebox_data: dict = json.loads(next(iter(json.loads(str(shoebox_data_tag.contents[0])).values())))
            json_data: dict = shoebox_data["d"]

        except (KeyError, TypeError):
            raise PageLoadError("Invalid / missing data on the page.")

        return Scraper._find_playlist_data_appletv_json_(json_data)

    @staticmethod
    def find_subtitles(main_playlist: M3U8, subtitles_filter: Union[list, None] = None) -> Iterator[SubtitlesData]:
        """
        Find and yield playlists within main_playlist for subtitles that match a filter.

        Args:
            main_playlist (M3U8): an M3U8 object of the main playlist.
            subtitles_filter (list, optional): A list of subtitles language codes (ISO 639-1) or names to use as a filter. Defaults to None.

        Yields:
            SubtitlesData: A NamedTuple with a matching playlist, and it's metadata:
            Language Code, Language Name, SubtitlesType, Playlist URL.
        """
        if subtitles_filter is not None:
            # Convert filters to lower-case for case-insensitive matching
            subtitles_filter = [f.lower() for f in subtitles_filter]

        for playlist in main_playlist.media:
            # Check whether playlist is valid and matches filter
            # "group_id" can be either ["subtitles_ak" / "subtitles_vod-ak-amt.tv.apple.com"] or
            # ["subtitles_ap2" / "subtitles_ap3" / "subtitles_vod-ap-amt.tv.apple.com" / "subtitles_vod-ap1-amt.tv.apple.com" / "subtitles_vod-ap3-amt.tv.apple.com"]
            if (playlist.type == "SUBTITLES") and (playlist.group_id in ("subtitles_ak", "subtitles_vod-ak-amt.tv.apple.com")):

                language_code: str = playlist.language
                language_name: str = playlist.name
                sub_type: SubtitlesType = SubtitlesType.NORMAL

                # Playlist does not match filter
                if subtitles_filter is not None and not (language_code.lower() in subtitles_filter or language_name in subtitles_filter):
                    continue

                # Change subtitles type to "Forced" / "Closed Captions" if needed.
                if playlist.forced == "YES":
                    sub_type = SubtitlesType.FORCED

                elif playlist.characteristics is not None and "public.accessibility" in playlist.characteristics:
                    sub_type = SubtitlesType.CC

                yield SubtitlesData(language_code, language_name, sub_type, playlist.uri)
