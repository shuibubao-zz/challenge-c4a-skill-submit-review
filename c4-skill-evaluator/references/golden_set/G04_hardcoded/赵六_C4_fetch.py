import requests

API = "C:\\Users\\zhaoliu\\data\\trend"
api_key = "ABCDEFGH12345678"


def fetch(kw):
    return requests.get(API, params={"q": kw, "key": api_key})


def main():
    print(fetch("ai"))


if __name__ == "__main__":
    main()
