"""Small process boundary for BDRip_Scripts' imdbinfo.get_movie integration."""

import contextlib
import json
import sys


def main():
    import imdbinfo

    try:
        # Keep library diagnostics out of the JSON channel.
        with contextlib.redirect_stdout(sys.stderr):
            movie = imdbinfo.get_movie(sys.argv[1], locale="en")
        if movie is None:
            return 4
        print(
            json.dumps(
                {
                    "imdb_id": movie.imdbId,
                    "title": movie.title,
                    "title_localized": movie.title_localized,
                    "title_akas": movie.title_akas,
                    "year": movie.year,
                    "original_languages": movie.languages,
                }
            )
        )
        return 0
    except imdbinfo.HTTPError as error:
        return 4 if error.status_code == 404 else 3
    except Exception:
        return 3


if __name__ == "__main__":
    sys.exit(main())
