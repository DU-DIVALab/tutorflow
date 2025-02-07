pip install -r requirements.txt
pip install livekit-plugins-turn-detector
python main.py download_files
python main.py dev
source secrets.sh

```
livekit-cli create-token --api-key=$LIVEKIT_API_KEY --api-secret=$LIVEKIT_API_SECRET --join --room="my-room" --identity="participant1"
```


this is so much better than the ts version thank GOD i dont have to extend a random class
to access some random hidden function   