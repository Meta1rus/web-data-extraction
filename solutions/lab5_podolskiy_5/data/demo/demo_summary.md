### JOBDIR: прерывание и возобновление

| Прогон | Причина остановки | Записей | Запросов | dupefilter/filtered | Время, с |
|---|---|---|---|---|---|
| 1 (CLOSESPIDER_ITEMCOUNT=30) | closespider_itemcount | 32 | 36 | — | 38.32 |
| 2 (возобновление из jobstate/) | finished | 32 | 35 | 17 | 30.97 |

Отпечатков в jobstate/requests.seen после прогона 1: 51. Уникальных SKU за два прогона: 64, повторно собранных во втором: 0.

Содержимое jobstate/ после прогона 1:

| Файл | Байт |
|---|---|
| jobstate/requests.queue/active.json | 27 |
| jobstate/requests.queue/scrapeme.live-f865497a6f0073e2c23e0e559fe35b7b/-10 | 7832 |
| jobstate/requests.queue/scrapeme.live-f865497a6f0073e2c23e0e559fe35b7b/0 | 311 |
| jobstate/requests.seen | 1122 |
| jobstate/spider.state | 5 |

### HTTPCACHE: два прогона подряд

| Прогон | Время, с | Записей | httpcache/store | httpcache/hit | httpcache/miss |
|---|---|---|---|---|---|
| cache_run1 | 46.69 | 48 | 52 | 0 | 52 |
| cache_run2 | 0.43 | 48 | 0 | 52 | 0 |
