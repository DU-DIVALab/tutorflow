## Tutorflow

A minimalistic frontend for interacting with [LiveKit Agents](https://docs.livekit.io/agents).

![Screenshot of the frontend application.](/.github/assets/frontent-screenshot.jpeg)


```
if "SQUARE" in room_name:
    mode = TeachingMode.USER_LED
elif "CIRCLE" in room_name:
    mode = TeachingMode.AGENT_LED
elif "TRIANGLE" in room_name:
    mode = TeachingMode.HAND_RAISE
```

## Setup

You'll need to make sure you have Git, Python, and NodeJS installed! Clone the repository with: `git clone https://github.com/DU-DIVALab/tutorflow` and ensure its up to date with `git pull`

### `backend/`

```
cd backend
pip install -r requirements.txt
python main.py download-files
python distill.py
vi secrets.sh
```
Now, you'll need to enter the secrets:

```
#!/usr/bin/env bash
# doesn't work lmfao, just copypaste

alias python='python3'
export LIVEKIT_URL='wss://org-project-ab1cd23e.livekit.cloud'   
export LIVEKIT_API_KEY='API0aaBBBcDEEfG' 
export LIVEKIT_API_SECRET='aB1Cde2a1BaCdD3DEfGh4IJKLMNOPQrS0TUV7WxYYZz'
export DEEPGRAM_API_KEY='1111aab22c33d4444444444e5fffff5g666h7ii8'
export OPENAI_API_KEY='sk-proj-these-are-dummy-values-btw'
```

`:wq!`, alright now let's cd out: `cd ..`

### `frontend/`

```
cd frontend
pnpm install
mv .env.example .env.local
```

You know the drill, `vi .env.local` blah blah blah. You also need your certs in the "certs" folder, `mkdir certs`, `mv etc/idk/wherever/they/are/* ./certs`


Cd out again `cd ..`

## Run

### backend
screen -S backend
cd backend
source secrets.sh
python main.py dev
Control + A; D


### frontend
screen -S frontend
cd frontend
sudo pnpm deploy-cci
Control + A; D
