python -m PyInstaller --onefile --windowed --add-data "accept.png;." --add-data "close.png;." --add-data "config.ini;." --add-data ".env;." main.py
copy close.png dist
copy accept.png dist