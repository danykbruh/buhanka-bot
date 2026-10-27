import sys
from pathlib import Path

# Модули бота лежат в bot/, тесты запускаются из корня репозитория: pytest bot/tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
