# Базовый образ Python
FROM python:3.11-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения (main.py и index.html)
COPY . .

# Открываем порт 8000
EXPOSE 8000

# Запускаем приложение
CMD ["python", "main.py"]