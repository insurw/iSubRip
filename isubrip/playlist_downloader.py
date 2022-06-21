import asyncio
import os

import aiohttp
import m3u8

from isubrip.enums import SubtitlesFormat
from isubrip.namedtuples import SubtitlesData, SubtitlesType, MovieData
from isubrip.subtitles import Subtitles
from isubrip.utils import format_title


class PlaylistDownloader:
    """A class for downloading & converting m3u8 playlists into subtitles."""
    def __init__(self, user_agent: str = None) -> None:
        """
        Create a new PlaylistDownloader instance.

        Args:
            user_agent (str): User agent to use when downloading. Uses default user-agent if not set.
        """
        self.session = aiohttp.ClientSession()

        if user_agent is not None:
            self.session.headers.update({"user-agent": user_agent})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    async def _download_segment(self, segment_url: str) -> str:
        """
        Download an m3u8 segment.

        Args:
            segment_url (str): Segment URL to download.

        Returns:
            str: Downloaded segment data as a string.
        """
        data = await self.session.get(segment_url)
        content = await data.read()
        return content.decode('utf-8')

    @staticmethod
    def _format_file_name(movie_title: str, release_year: int, language_code: str, subtitles_type: SubtitlesType, file_format: SubtitlesFormat) -> str:
        """Generate file name for a subtitles file.

        Args:
            movie_title (str): Movie title.
            release_year(int): Movie release year.
            language_code (str): Subtitles language code.
            subtitles_type (SubtitlesType): Subtitles type.

        Returns:
            str: A formatted file name (does not include a file extension).
        """
        # Add release year only if it's not already included in the title
        movie_release_year_str = '.' + str(release_year) if str(release_year) not in movie_title else ''
        file_name = f"{format_title(movie_title)}{movie_release_year_str}.iT.WEB.{language_code}"

        # Add subtitles type to file name if it's not `NORMAL` (ex: `FORCED`)
        if subtitles_type is not SubtitlesType.NORMAL:
            file_name += f".{subtitles_type.name.lower()}"

        # Add file format to file name (ex: ".vtt")
        file_name += f".{file_format.name.lower()}"
        return file_name

    def close(self) -> None:
        """Close aiohttp session."""
        async_loop = asyncio.get_event_loop()
        close_task = async_loop.create_task(self.session.close())
        async_loop.run_until_complete(asyncio.gather(close_task))

    def get_subtitles(self, subtitles_data: SubtitlesData) -> Subtitles:
        """
        Get a subtitles object parsed from a playlist.

        Args:
            subtitles_data (SubtitlesData): A SubtitlesData namedtuple with information about the subtitles.

        Returns:
            Subtitles: A Subtitles object representing the subtitles.
        """
        subtitles = Subtitles(subtitles_data.language_code)
        playlist = m3u8.load(subtitles_data.playlist_url)

        async_loop = asyncio.get_event_loop()
        async_tasks = [async_loop.create_task(self._download_segment(segment.absolute_uri)) for segment in playlist.segments]
        segments = async_loop.run_until_complete(asyncio.gather(*async_tasks))

        for segment in segments:
            subtitles.append_subtitles(Subtitles.loads(segment))

        return subtitles

    def download_subtitles(self, movie_data: MovieData, subtitles_data: SubtitlesData, output_dir: str, file_format: SubtitlesFormat = SubtitlesFormat.VTT) -> str:
        """
        Download a subtitles file from a playlist.

        Args:
            movie_data (MovieData): A MovieData namedtuple with information about the movie.
            subtitles_data (SubtitlesData): A SubtitlesData namedtuple with information about the subtitles.
            output_dir (str): Path to output directory (where the file will be saved).
            file_format (SubtitlesFormat, optional): File format to use for the downloaded file. Defaults to `VTT`.

        Returns:
            str: Path to the downloaded subtitles file.
        """
        file_name = self._format_file_name(
            movie_data.name,
            movie_data.release_year,
            subtitles_data.language_code,
            subtitles_data.subtitles_type,
            file_format)
        path = os.path.join(output_dir, file_name)

        with open(path, 'w', encoding="utf-8") as f:
            f.write(self.get_subtitles(subtitles_data).dumps(file_format))

        return path
