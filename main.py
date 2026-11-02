"""Project root launcher.

Adds src/ to sys.path and runs the bot. Using this file (python main.py)
does not depend on `pip install -e .` having succeeded, so it is the
reliable way to start the bot on any machine, including Windows.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from avitobot.bot import main

if __name__ == "__main__":
    main()