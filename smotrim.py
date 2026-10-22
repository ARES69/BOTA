import urllib.request
import json

print("Загружаю данные с Bybit... (это займет пару секунд)")
url = "https://api.bybit.com/v5/market/tickers?category=linear"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})

try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        
    # Берем только те, где ставка положительная (нам платят)
    # у некоторых инструментов fundingRate может быть пустой строкой — пропускаем
    def rate_of(item):
        try:
            return float(item.get('fundingRate') or 0)
        except (TypeError, ValueError):
            return 0.0

    positive_funding = [
        item for item in data['result']['list']
        if rate_of(item) > 0
    ]
    
    # Сортируем от самой высокой ставки к низкой
    positive_funding.sort(key=rate_of, reverse=True)
    
    print("\n🏆 ТОП-5 монет, которые ПЛАТЯТ шортам прямо сейчас:\n")
    print(f"{'Монета':<12} | {'Ставка за интервал':<12} | {'Годовых (примерно)':<20}")
    print("-" * 50)
    
    for item in positive_funding[:5]:
        symbol = item['symbol']
        rate_8h = rate_of(item) * 100
        # грубая прикидка: считаем интервал 8ч (у части монет он 4ч — годовых будет вдвое больше)
        hours = float(item.get('fundingIntervalHours') or 8)
        apy = rate_8h * (24 / hours) * 365
        
        print(f"{symbol:<12} | {rate_8h:>9.4f}%    | {apy:>17.1f}%")
        
except Exception as e:
    print(f"Ошибка: {e}")
