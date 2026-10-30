# Integration review (2026-10-30): NOT ACCEPTED as X4 Pro diagnosis

The alternate UcIdleHigh polling hypothesis in this exploratory agent report
is contradicted by the matching SSD1677 factory ELF, actual render task in
the freeink::s_epdRefreshDone semaphore wait, and the working QEMU 0042
GPIO6/PCPU_INT repair. Preserved only for historical traceability.

---

# 0037 — Аудит render-пайплайна YBX X4 Pro: где зависает render-таск и почему уведомления «не будят»

Worktree: `ybx-qwen38-six-20261030/render-pipeline`. SDK-пути ниже — read-only
чекаут `fw-092c2be5-symbols-20261029` (символы прошивки). Никаких изменений
прошивки, QEMU или чужих worktree; этот отчёт + `0037-render-busy-poll-check.py` —
единственные новые файлы.

## 1. Итог (одним абзацем)

Render-таск не заблокирован ни на `renderingMutex`, ни на `ulTaskNotifyTake`, ни
на семафоре. Он крутится в **без-таймаутном опросе BUSY** —
`EpdBus::waitBusy(BusyPolarity::UcIdleHigh)`
(`freeink-sdk/.../bus/EpdBus.cpp:342–358`), удерживая `renderingMutex` внутри
первого же рендера. В QEMU пин BUSY (GPIO6) сконфигурирован входом и читается
LOW (`input-lo=0x0004609b`, бит6=0 — см. 0042), а ветка UcIdleHigh трактует
LOW как «занято» и ждёт HIGH **без ограничения по времени**. Поэтому:
кадры последней волны записаны (MASTER_ACTIVATION прошёл), счётчики команд
SPI дальше не растут, а каждое последующее `xTaskNotify` только накапливает
`ulNotifiedValue` (1→2) — таск не в notification-wait, ядро по договору
FreeRTOS не делает wake/INTSETX. Flat INTSETX — **следствие**, а не причина;
теория сломанного cross-core IPC опровергнута (п.4).

## 2. Полный путь вызова с точными строками

1. `src/activities/ActivityManager.cpp:513–514` — `requestUpdateAndWait()`:
   `xTaskNotify(renderTaskHandle, 1, eIncrement)` → `ulTaskNotifyTake(pdTRUE, portMAX_DELAY)`.
2. `ActivityManager.cpp:80` — render-таск в `ulTaskNotifyTake(pdTRUE, portMAX_DELAY)`
   (единственный легальный способ тут «спать с pvContainer=0 у обоих списков»).
3. `ActivityManager.cpp:83` — `RenderLock lock` берёт `renderingMutex`
   (`xSemaphoreCreateMutex` на `:40`) с `portMAX_DELAY` (take на `:520`).
4. `ActivityManager.cpp:90` — `currentActivity->render(std::move(lock))`:
   **мьютекс удерживается всё это время**, включая обновление дисплея.
5. Обновление уходит в драйвер панели. Выбор драйвера для X4 Pro — рантайм
   (`freeink-sdk/.../FreeInkDisplay.cpp:199–232`, PanelSel::X4):
   - `ACTIVE.displayController == UC8179` → `uc8179Driver()` (`:201–204`);
   - `== UC8279` → `uc8279X4Driver()` (`:209–212`);
   - иначе compile-time `#elif` → `ssd1677Driver()` (`:231–232`).
   Значение ставит boot-time probe шины (`XteinkDetect`), т.е. разные загрузки
   в QEMU могут выбрать разные драйверы — это объясняет различие профилей 0036
   (ISR-семафор) и 0044 (delay-list poll).
6. Полярность BUSY по драйверам:
   - `Ssd1677Driver.h:86` — ActiveHigh (есть bail 30 s, `EpdBus.cpp:306–317`);
   - `Uc8179Driver.h:64` и `Uc8279X4Driver.h:69` — **UcIdleHigh**;
   - `Uc8279(X3)` — X3TwoPhase; `Ssd2677`/`M5` — ActiveLow.
7. Единственный UNTIMED блокирующий примитив во всём пути:

   ```cpp
   // EpdBus.cpp:342–358
   } else if (p == BusyPolarity::UcIdleHigh) {
     delay(1);
     while (digitalRead(_pins.busy) == LOW) {   // :349 — нет `millis()-start>30000 break`
       busyIdle(longWait, LOW, 1);              // → delay(1) (EpdBus.h:222–227)
       ...
     }
   }
   ```

   Остальные ветки (`:306–317`, `:318–341`, `:359–377`) и ISR-путь
   `waitRefreshComplete` (`:388–468`, `xSemaphoreTake(s_epdRefreshDone, 20/30000 ms)`)
   ограничены ~30 с. Комментарий SDK прямо говорит: «There is deliberately no
   fixed millisecond timeout on this controller path».
8. PIN: `BoardConfig.h:1835–1849` — XTEINK_X4_PRO, BUSY=GPIO6 (профиль по
   умолчанию `DisplayController::SSD1677`; probe может переключить на UC8179/UC8279).
9. Точки вызова в `Uc8279X4Driver.cpp`: `waitBusy` на `:388, :525 (" 8279x4_POF"),
   :547, :596/:624, :684, :690, :743, :756, :768, :805`; `waitRefreshComplete`
   на `:427` и `:514`.

## 3. Почему «картинка есть, а следующий рендер нет»

Последняя волна кадров до записи в RAM и MASTER_ACTIVATION проходит — экран
заморожен с реальным содержимым. Сразу после DRF/POF таск входит в
`waitBusy(UcIdleHigh)` и остаётся там навсегда, т.к. модель QEMU держит GPIO6
входом LOW. Таск:

- не на `xDelayedTaskList` из-за `vTaskDelay` между рендерами, а внутри
  `delay(1)` каждого шага опроса → xStateListItem на `xDelayedTaskList1`
  (`0x3fcac1a8`) — ровно как в 0044;
- не на каком-либо event-листе → `xEventListItem.pvContainer = 0` — ровно как в 0044;
- удерживает `renderingMutex` → все `RenderLock`-претенденты (включая main loop)
  сидят на `portMAX_DELAY` take (`ActivityManager.cpp:520`).

## 4. Проверка гипотезы пользователя о notification/wake — завершена, гипотеза опровергнута как первопричина

Наблюдения 0045/0046 верны, но их трактовка — нет:

- `xTaskNotifyGive` пишет `ulNotifiedValue` 0→1 и возвращает pdPASS — да.
- Таск не появляется в `xPendingReadyList`, INTSETX CPU0 не растёт — да.
- **Но это корректное поведение ядра**, если целевой таск НЕ в notification-wait
  (`ucNotifyState == taskNOT_WAITING_NOTIFICATION(0)` на момент вызова):
  FreeRTOS тогда только накапливает значение — без removal из списков, без
  insert в ready/pending-ready, без cross-core yield, и, соответственно, без
  единого INTSETX. Накопление 1→2 (`ulNotifiedValue=0x2` в 0044) — прямой
  отпечаток этого пути: первое уведомление записано таску, который его не ждал,
  второе добавилось поверх.
- Доказательство, что IPC-путь жив: 0041 CPU0 `INTERRUPT=0x00000020` (bit5
  ExtINT) — ядро 0 реально обслуживает внешние прерывания; 0042 (свежий билд
  QEMU sha 4106cff2) даёт **frames 4→5, ram-writes 384000→480000, команды
  82→99, новый frame SHA `8b81cec4…`** после инъекции — т.е. навигация там
  смогла породить новый кадр; «сломанный cross-core wake» не воспроизводится
  как необходимое объяснение.
- Формальная нестыковка в постановке 0044 («task blocked in a timed semaphore
  take»): блокировка на семафоре поставила бы `xEventListItem.pvContainer` на
  очередь ожидания; в том же 0044 `pvContainer=0x0`. Отсюда единственная
  непротиворечивая интерпретация TCB-снимка — `delay(1)` внутри опроса BUSY.

## 5. Таблица доказательств

| # | Источник | Факт | Вывод |
|---|---|---|---|
| 1 | `qa-20261029/s3-x4pro-epd-isr-semaphore-0041.json` | before/during/after: frames=2, commands=41, ram-writes=144000, SHA e79c2ad5 — заморожены; CPU0 INTERRUPT=0x20; gpio_irq_level QMP-свойство отсутствует в том билде | Счётчики стоят; прерывания CPU0 живы |
| 2 | `.../s3-x4pro-gpio6-pcpu-irq-0042-2s-final.json` | after: frames 4→5, ram-writes→480000, commands 82→99, SHA→8b81cec4…; `input-lo 0x6000403C=0x0004609b` (бит6=0), `enable-lo=0x00047b36` (бит6=1), `pin6-config=0x0` | GPIO6 — вход, читается LOW = «вечно занят» для UcIdleHigh; в новом билде прогресс кадров возможен |
| 3 | 0044 GDB (только из текста постановки; файлы `ybx-rtos-crosscore-0044.log` на диске не найдены) | TCB 0x3fcecc68: `ulNotifiedValue=0x2`, `ucNotifyState=1`, `xEventListItem.pvContainer=0`, `xStateListItem.pvContainer=0x3fcac1a8` (xDelayedTaskList1) | Паттерн «spит в delay(1), не в notify-take, не на семафоре» |
| 4 | `EpdBus.cpp:342–358` | Единственный untimed-опрос; комментарий «deliberately no timeout» | Кандидат №1 точки зависания |
| 5 | `Uc8179Driver.h:64`, `Uc8279X4Driver.h:69` vs `Ssd1677Driver.h:86` | UcIdleHigh у обеих UC-веток X4 Pro; ActiveHigh (30 s bail) у SSD1677 | Зависание возможно только при UC-варианте, выбранном probe |
| 6 | `FreeInkDisplay.cpp:199–232` | Рантайм-роутинг PanelSel::X4 | Разные boot → разные драйверы → разные профили 0036/0044 |
| 7 | `ActivityManager.cpp:80/83/90/513–520` | Мьютекс удерживается через весь render, включая refresh | Один застрявший render блокирует всех |

## 6. Мини-инструменты диагностики (GDB, без правок кода)

1. Полярность и пин активного драйвера: `print EpdBus` объекта дисплея →
   `_busy` (ожидание: 3 = UcIdleHigh) и `_pins.busy` (ожидание: 6).
2. Что выбрал probe: `print BoardConfig::ACTIVE.displayController`
   (SSD1677/UC8179/UC8279). Ключ к расхождению 0036↔0044.
3. Подтверждение точки зависания: saved-PC из `pxTopOfStack` render-таска →
   `info symbol` — должен указать внутри `EpdBus::waitBusy` / `delay`;
   либо брейкпоинт на `EpdBus.cpp:349` и счётчик попаданий.
4. Уровень BUSY во время зависания: MMIO `0x6000403C` бит6 (уже снято в 0042: 0).
5. `nm` сверка `s_epdRefreshDone` (в другом boot `0x3fca1da8`) — если таск не
   на нём, ISR-теория закрыта (и закрыта: pvContainer=0).
6. На стороне QEMU: проверить, как модель панели X4 Pro управляет GPIO6 после
   MASTER_ACTIVATION (отсутствующий pull-up / незадеасерт уровня — см. патчи
   0020/0022 этого worktree).

## 7. Рекомендуемое одиночное следующее изменение

Одно точечное, в модели QEMU (opt-in патч внутри worktree, не трогая прошивку):
**после MASTER_ACTIVATION переводить GPIO6 BUSY в idle-HIGH для UC-variant X4
Pro** (или обеспечить pull-up при конфигурации входом), затем перезагрузить
гостя и проверить критерий: в не-GDB прогоне native RAM SHA меняется и
refresh count растёт после тапа — это одновременно закрывает gate навигации.
Если после этого INTSETX/wake так и не понадобятся — cross-core-теория
похоронена окончательно; если таск всё же не проснётся — следующий шаг
(и только следующий) — `print BoardConfig::ACTIVE.displayController`, чтобы
исключить выбор SSD1677-ветки probe.

## 8. Файлы, изменённые этой сессией

- `tools/ybx-system-emu/experiments/0037-agent-render-pipeline.md` — этот отчёт (новый).
- `tools/ybx-system-emu/experiments/0037-render-busy-poll-check.py` — независимый host-скрипт:
  (а) парсит QA JSON и печатает before/after счётчики, SHA и бит6 GPIO6;
  (б) моделирует три ветки `waitBusy` с застрявшим LOW и показывает, что только
  UcIdleHigh висит бесконечно (host-only, без Docker/guest).
  Запуск этой сессии: **RESULT: PASS** — ActiveHigh=idle, ActiveLow/X3TwoPhase=bail30s,
  UcIdleHigh=HANG(>120 s); 0041 frozen [2,2,2]/1 SHA; 0042 frames 4→5,
  SHA e79c2ad5→8b81cec4, `guest_epd_frame_sha_changed_after_contact=true`,
  `guest_epd_frame_count_increased=true`; GPIO6 bit6=0.

Ничего больше не создавалось, не коммитилось и не публиковалось.
