# Проверка на сервере

Локальные тесты запрещены запросом пользователя. Этот список выполняется только
после установки на целевой сервер. Не считайте функции подтверждёнными до проверки.

## 1. До пользовательского доступа

- [ ] `bash /opt/fermde/deploy/check-server.sh` завершился без ошибок.
- [ ] Сайт доступен по HTTPS без предупреждения сертификата.
- [ ] Вход администратора, неверный пароль и выход работают.
- [ ] Запрос без входа к `/api/devices` возвращает 401.
- [ ] Порты 8090, 5037, 5039, 5554/5555 и X11 недоступны из интернета.
      `ss` показывает привязку к localhost; сетевой firewall проверить с другого ПК.
- [ ] API-ключ введён через Настройки и не появляется в ответе настроек/журналах.
- [ ] Проверка баланса FloppyData проходит.

## 2. Один телефон

- [ ] Создание чистого устройства завершается, Google Play присутствует.
- [ ] Запуск даёт статус «Работает», браузер показывает кадры.
- [ ] Тап, удержание, перетаскивание, колесо, навигация работают.
- [ ] Кириллица и вставка текста работают в поле приложения.
- [ ] Проверены три режима качества и включение/выключение звука.
- [ ] Перезагрузка Android сохраняет данные и возвращает статус «Работает».
- [ ] Замена прокси доступна только администратору и только остановленному телефону.
- [ ] APK устанавливается, обычный файл появляется в Download.
- [ ] Закрытие вкладки на 2 минуты не останавливает телефон.
- [ ] Повторное открытие сохраняет приложение и данные.
- [ ] Остановка/запуск сохраняет данные и прокси-сессию.
- [ ] Рестарт службы панели не останавливает телефон.
- [ ] Открытие того же телефона во второй вкладке даёт понятное сообщение.

## 3. Прокси и отсутствие прямого выхода

Проверяйте на устройстве без реальных банковских аккаунтов.

```bash
# Проверка IP через тот же namespace (замените 1 на ID в панели)
ip netns exec fermde-1 curl -fsS --max-time 20 https://api.ipify.org
# Маршруты и правила
ip netns exec fermde-1 ip route
ip netns exec fermde-1 nft list ruleset
```

- [ ] IP из браузера Android совпадает с проверкой панели, отличается от IP сервера.
- [ ] DNS-запросы не выходят напрямую с хоста от этого namespace.
- [ ] UDP-приложения работают либо явно не проходят; прямого fallback нет.
- [ ] IPv6 не даёт обхода прокси.
- [ ] При двух устройствах проверены IP; совпадение отображается предупреждением.

Проверка отказа (временно отключает интернет только телефона 1):

```bash
systemctl stop fermde-proxy-1
ip netns exec fermde-1 curl -fsS --max-time 10 https://api.ipify.org
# Ожидается ошибка/таймаут, а НЕ IP сервера.
systemctl start fermde-proxy-1
```

- [ ] Телефон продолжает работать во время отказа.
- [ ] После восстановления интернет возвращается.
- [ ] Просмотр экрана продолжает работать независимо от прокси.

## 4. Владение и квоты

- [ ] Создать пользователей Alice и Bob, войти в разных профилях браузера.
- [ ] Каждый видит только свои телефоны.
- [ ] Подстановка чужого ID в API, загрузку файла и WebSocket не даёт доступа.
- [ ] Отключение пользователя отзывает открытый просмотр в течение нескольких секунд.
- [ ] Квота создания блокирует лишнее устройство; не удаляет существующее.
- [ ] Два одновременных запроса запуска не обходят общий лимит.
- [ ] Превышение VRAM-порога запрещает запуск, существующие телефоны продолжают работу.
- [ ] Существующий старый `phone` сохранён вне панели, учтён при оценке ресурсов.

## 5. Нагрузка

Сначала 1 телефон, затем 2, 3, 4 с видео/прокруткой и открытыми браузерными экранами.
Не увеличивайте максимум выше 4 до завершения измерений.

```bash
nvidia-smi --query-gpu=timestamp,memory.total,memory.used,memory.free,utilization.gpu --format=csv -l 2
```

- [ ] Записать пик VRAM и минимум свободной памяти в каждом режиме, включая загрузку.
- [ ] Проверить задержку без VPN и частоту кадров в разных браузерах.
- [ ] Проверить одновременную работу с выбранной LLM отдельно.
- [ ] Уточнить бюджет одного запуска; при необходимости СНИЗИТЬ максимум.

## 6. Сохранность

- [ ] Архив конфигурации создаётся, доступен только root.
- [ ] Бэкап работающего устройства отклоняется без его остановки.
- [ ] Бэкап явно остановленного устройства создаётся.
- [ ] Удаление требует ввода подтверждения и удаляет только выбранное устройство.
- [ ] Восстановление проверено на тестовых данных по README.
- [ ] После перезагрузки сервера устройства запускаются вручную; данные сохранились.

## Отчёт

Прислать вывод check-server, журнал проблемного устройства, модель браузера и
результаты замеров. Не присылать `/etc/fermde/secret.key`, API-ключ, proxy URL с
паролем, базу данных или архив резервной копии.
# Viewer and DNS update

## File exchange

Open Files from navigation or the viewer. Upload a file with a Unicode name,
download it, and compare its bytes. Open the same account in another browser:
the file must still be listed. A different account must not see or download it,
even when given its download URL. Download URLs require an active login.

Select a running phone as the destination and send the file. It must appear in
Android Download. Select that phone's Download folder, retrieve a file and
confirm it is both saved to the account's server folder and downloaded to the PC.
Existing APK installation through the viewer's upload control remains separate;
the file exchange's Send action copies a file without installing it.

Limits: 512 MiB per file, 5 GiB per account. Files are stored under
`/var/lib/fermde/files/<user-id>` independently of phone lifetimes. Include this
directory in storage backups; the existing configuration and device backup
scripts do not archive these account files. Browser downloads are explicit,
not continuous synchronization with a Windows filesystem directory.

## Concurrent lifecycle checks

After updating, start two stopped phones without waiting for the first to boot.
The `lifecycle` journal must show both `phase=waiting_adb` entries before the
first `phase=ready`. Admission and host setup remain serialized; Android boot,
stop, deletion, proxy requests and reconciliation are isolated by device.
Pending boots reserve an additional device budget during admission, so a burst
of starts can be refused until earlier boots complete even if the GPU is still
mostly empty. This is intentional protection against delayed VRAM allocation.

On panel restart, an interrupted `stopping` or `deleting` operation is completed
only when neither a panel task nor a root device lock owns that device. Verify
that an inactive phone with a leftover socat bridge becomes `stopped` and that
the bridge exits. Active unrelated devices must keep running. Device backups
now use the same per-device root lock as lifecycle mutations.

Run `bash deploy/update.sh` from `/opt/fermde-src` on the server. The script runs
the Python checks on the server and updates panel code without stopping phones.
Then explicitly stop and start the test phone from the panel (Android's Reboot
button does not recreate its namespace). Existing app data must remain intact.

- Refresh with Ctrl+F5. Check cards, settings, users and the balance display.
- Open the viewer: it should fill the viewport. Test fit, 125%, 200%, fullscreen,
  touch coordinates near all four corners, clipboard, file upload and audio.
- At narrow viewport widths the controls move below the screen.
- Close the viewer, reopen and confirm the phone stayed running.
- Run `bash deploy/diagnose-phone.sh 1`: DNS and external IP must both succeed.
- Open a site in Android Chrome; namespace probing alone does not prove guest
  connectivity. Compare the browser's external IP with the panel.
- Stop `fermde-proxy-1` briefly: fresh requests must fail, with no direct fallback.
  Start it again and recheck IP; the earlier error must clear after success.

DNS listens only on namespace loopback and forwards to Cloudflare DNS over TCP
through tun2socks. This removes DNS's dependency on SOCKS5 UDP relay support;
it does not add UDP support to a provider that lacks it. The TUN route and firewall
remain in force. Interface binding follows the upstream
[tun2socks configuration](https://github.com/xjasonlyu/tun2socks/wiki/Examples).
