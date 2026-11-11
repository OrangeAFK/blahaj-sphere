import sys

from app.voice.models import download

if __name__ == "__main__":
    if sys.argv[1:] == ["download"]:
        download()
    else:
        print("usage: python -m app.voice download")
        sys.exit(2)
