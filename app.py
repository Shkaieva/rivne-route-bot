import os
import asyncio
import threading
from flask import Flask
import logging

from bot_fixed import main

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health_check():
    return "Bot is running", 200

def run_bot():
    asyncio.run(main())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logging.info(f"Starting Flask server on port {port}")

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host='0.0.0.0', port=port)