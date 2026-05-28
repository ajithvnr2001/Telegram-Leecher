# copyright 2023 © Xron Trix | https://github.com/Xrontrix10


# @title 🖥️ Main Colab Leech Code

# @title Main Code
# @markdown <div><center><img src="https://user-images.githubusercontent.com/125879861/255391401-371f3a64-732d-4954-ac0f-4f093a6605e1.png" height=80></center></div>
# @markdown <center><h4><a href="https://github.com/XronTrix10/Telegram-Leecher/wiki/INSTRUCTIONS">READ</a> How to use</h4></center>

# @markdown <br>
# @markdown ### 🤖 Telegram Bot Configuration *(required)*
API_ID = 0  # @param {type: "integer"}
API_HASH = ""  # @param {type: "string"}
BOT_TOKEN = ""  # @param {type: "string"}
USER_ID = 0  # @param {type: "integer"}
DUMP_ID = 0  # @param {type: "integer"}

# @markdown ---
# @markdown ### ☁️ S3 / Wasabi / B2 Configuration *(optional — needed only for `/s3upload` and `/s3leech`)*
# @markdown
# @markdown Works with **AWS, Wasabi, Backblaze B2, MinIO** and any other S3-compatible service. Leave the
# @markdown five fields below blank if you don't plan to use S3.
# @markdown
# @markdown **Endpoint examples:**
# @markdown - **AWS S3:** leave `S3_ENDPOINT_URL` empty
# @markdown - **Wasabi:** `https://s3.ap-northeast-1.wasabisys.com` (pick your region)
# @markdown - **Backblaze B2:** `https://s3.us-west-002.backblazeb2.com`
# @markdown - **MinIO / self-hosted:** `https://your-minio.example.com`
S3_ACCESS_KEY = ""  # @param {type: "string"}
S3_SECRET_KEY = ""  # @param {type: "string"}
S3_BUCKET_NAME = ""  # @param {type: "string"}
S3_ENDPOINT_URL = ""  # @param {type: "string"}
S3_REGION = "us-east-1"  # @param {type: "string"}

# @markdown ---
# @markdown ### 📚 Quick command cheat sheet
# @markdown - `/tupload` &nbsp;&nbsp;— leech links to Telegram
# @markdown - `/gdupload` — mirror to Google Drive
# @markdown - `/ytupload` — YouTube / yt-dlp leech
# @markdown - `/drupload` — leech a local Colab folder
# @markdown - `/s3upload` &nbsp;**☁️ NEW** &nbsp;— mirror downloads to your S3 / Wasabi bucket (>2 GB OK, multipart)
# @markdown - `/s3leech` &nbsp;&nbsp;**📥 NEW** &nbsp;— leech `s3://bucket/key` or `s3://bucket/folder/` to Telegram
# @markdown - `/s3bucket <name>` — change destination bucket on the fly
# @markdown - `/s3prefix <folder>` — set a destination key prefix
# @markdown
# @markdown All S3 transfers are logged to `/content/Telegram-Leecher/s3teletracker.json` (both `uploaded` and `downloaded`).


import subprocess, time, json, shutil, os
from IPython.display import clear_output
from threading import Thread

Working = True

banner = '''

 ____   ____.______  ._______  .______       _____._.______  .___  ____   ____
 \\   \\_/   /: __   \\ : .___  \\ :      \\      \\__ _:|: __   \\ : __| \\   \\_/   /
  \\___ ___/ |  \\____|| :   |  ||       |       |  :||  \\____|| : |  \\___ ___/ 
  /   _   \\ |   :  \\ |     :  ||   |   |       |   ||   :  \\ |   |  /   _   \\ 
 /___/ \\___\\|   |___\\ \\_. ___/ |___|   |       |   ||   |___\\|   | /___/ \\___\\
            |___|       :/         |___|       |___||___|    |___|            
                        :                                                     
                                                                              
 
              _____     __     __     __              __          
             / ___/__  / /__ _/ /    / / ___ ___ ____/ /  ___ ____
            / /__/ _ \\/ / _ `/ _ \\  / /_/ -_) -_) __/ _ \\/ -_) __/
            \\___/\\___/_/\\_,_/_.__/ /____|__/\\__/\\__/_//_/\\__/_/   

                                                

'''

print(banner)

def Loading():
    white = 37
    black = 0
    while Working:
        print("\r" + "░"*white + "▒▒"+ "▓"*black + "▒▒" + "░"*white, end="")
        black = (black + 2) % 75
        white = (white -1) if white != 0 else 37
        time.sleep(2)
    clear_output()


_Thread = Thread(target=Loading, name="Prepare", args=())
_Thread.start()

if len(str(DUMP_ID)) == 10 and "-100" not in str(DUMP_ID):
    n_dump = "-100" + str(DUMP_ID)
    DUMP_ID = int(n_dump)

if os.path.exists("/content/sample_data"):
    shutil.rmtree("/content/sample_data")

cmd = "git clone https://github.com/XronTrix10/Telegram-Leecher"
proc = subprocess.run(cmd, shell=True)
cmd = "apt update && apt install ffmpeg aria2"
proc = subprocess.run(cmd, shell=True)
cmd = "pip3 install -r /content/Telegram-Leecher/requirements.txt"
proc = subprocess.run(cmd, shell=True)

# Validate S3 config (warn-only — bot still starts; /s3* commands will tell the user
# what's missing if they're triggered without complete credentials).
_s3_partial = any([S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET_NAME])
_s3_complete = all([S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET_NAME])
if _s3_partial and not _s3_complete:
    print(
        "\n⚠️  S3 config is partially set — /s3upload and /s3leech will be unavailable until\n"
        "   S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET_NAME are all filled in.\n"
    )
elif _s3_complete:
    _endpoint_label = S3_ENDPOINT_URL if S3_ENDPOINT_URL else "AWS S3 (default)"
    print(f"\n☁️  S3 enabled → bucket=<{S3_BUCKET_NAME}> region=<{S3_REGION}> endpoint=<{_endpoint_label}>\n")

credentials = {
    "API_ID": API_ID,
    "API_HASH": API_HASH,
    "BOT_TOKEN": BOT_TOKEN,
    "USER_ID": USER_ID,
    "DUMP_ID": DUMP_ID,
    "S3_ACCESS_KEY": S3_ACCESS_KEY,
    "S3_SECRET_KEY": S3_SECRET_KEY,
    "S3_BUCKET_NAME": S3_BUCKET_NAME,
    "S3_ENDPOINT_URL": S3_ENDPOINT_URL,
    "S3_REGION": S3_REGION,
}

with open('/content/Telegram-Leecher/credentials.json', 'w') as file:
    file.write(json.dumps(credentials))

Working = False

if os.path.exists("/content/Telegram-Leecher/my_bot.session"):
    os.remove("/content/Telegram-Leecher/my_bot.session") # Remove previous bot session
    
print("\rStarting Bot....")

!cd /content/Telegram-Leecher/ && python3 -m colab_leecher #type:ignore
