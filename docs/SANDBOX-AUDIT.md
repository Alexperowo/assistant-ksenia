# Аудит production-песочницы Windows

Дата проверки: 30 августа 2026 года. Цель документа — отделить фактически доступную системную изоляцию от ограничений, которые только уменьшают ущерб, но не образуют security boundary.

## Текущее состояние

| Требование | Статус | Подтверждённое поведение |
|---|---|---|
| Permission Broker перед исполнением | DONE | Решение о праве действия остаётся вне command backend. |
| Fail-closed при отсутствии sandbox | DONE | `DeveloperRunner` выбирает `disabled` по умолчанию; неизвестная или повреждённая конфигурация не создаёт процесс. |
| Явное отделение legacy host execution | DONE | `unsafe_host` требует точной локальной строки признания риска и не называется песочницей. |
| Проверка программы, argv и cwd | DONE | Shell, inline Python/Node, опасные Git-команды и каталог вне `workspace` отклоняются до backend-а. |
| Workspace RW и остальная файловая система deny/RO | MISSING | Политика пути до запуска не ограничивает уже запущенный Python/Node на уровне ОС. |
| Network deny-by-default / allowlist | MISSING | Без AppContainer/VM дочерний код наследует сеть пользователя. |
| Process tree, CPU, RAM, timeout, UI | PARTIAL | Timeout есть только у legacy runner; Job Object ещё не подключён и сам по себе не ограничивает файлы/сеть. |
| Cooperative task cancellation | PARTIAL | Отмена до запуска работает; mid-operation остановка command process tree не заявляется. |
| Production backend на компьютере Александра | BLOCKED | Аппаратная виртуализация выключена в UEFI; WSL и контейнерный runtime не установлены, гипервизор не активен. |

Безопасная конфигурация остаётся:

```json
"execution": {
  "backend": "disabled",
  "unsafe_host_acknowledgement": ""
}
```

## Проверка компьютера

Read-only инвентаризация показала:

- Windows build `26200`, AMD64;
- `VirtualizationFirmwareEnabled=False` и `HyperVisorPresent=False`;
- WSL сообщает, что подсистема не установлена;
- `docker`, `podman` и Windows Sandbox command отсутствуют;
- доступна служба `HvHost`, но она остановлена;
- запрос состояния optional features без повышения прав корректно отказал и не изменил систему.

Это не переносимая константа продукта и не должно попадать в исполняемый код. Состояние нужно повторно измерить после включения AMD-V/SVM в UEFI и перезагрузки.

## Рассмотренные варианты

### Windows Sandbox / Hyper-V

Это предпочтительная изоляция недоверенного Python/Node на данном ПК: отдельная виртуализированная среда, выключаемая сеть, ограниченная память и явно отображаемая рабочая папка. Она требует включённой виртуализации в BIOS/UEFI и Windows-компонента. Сейчас prerequisite не выполнен, поэтому backend нельзя реализовать и принять живым тестом без отдельного действия Александра и перезагрузки.

Перед production-подключением необходимо доказать не только запуск GUI `.wsb`, но и надёжный двусторонний протокол worker-а: staging workspace, stdout/stderr/result, timeout/cancel, уничтожение среды, отсутствие clipboard/audio/video/printer redirection, network off для `SANDBOX_OFFLINE` и allowlist gateway для `SANDBOX_NETWORKED`.

### AppContainer

Microsoft документирует AppContainer как kernel-enforced process/resource isolation boundary: без capabilities процесс не получает сеть, а доступ к файлам задаётся AppContainer SID/DACL. Вместе с Job Object это технически покрывает filesystem, network, UI, process tree и resource limits без аппаратной виртуализации.

Однако два доступных пути пока не приняты:

- самостоятельный launcher через `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` требует корректного управления profile SID, ACL, inherited handles, окружением, low integrity, cleanup и всеми ветками ошибки;
- системные `Experimental_CreateProcessInSandbox`/`Experimental_CreateProcessAsUserInSandbox` дают Bound File System и composable policy, но Microsoft прямо помечает API experimental, публичного header нет, спецификация FlatBuffer версии `0.1.0` может измениться.

Прототип Microsoft Execution Containers 0.8.0 уже прошёл отрицательные filesystem/network/child-process проверки, но upstream сам запрещает считать preview profiles security boundary. Его нельзя переименовать в production backend.

### Job Object / restricted token

Job Object полезен и обязателен для управления деревом процессов, kill-on-close, RAM/CPU/time limits и UI restrictions. Restricted token уменьшает права процесса. Ни один из них отдельно не задаёт workspace-only filesystem и network deny; поэтому backend с одним Job Object или токеном оставался бы `unsafe_host`, а не `SANDBOX_OFFLINE`.

### WSL2 / container runtime

WSL2 или Hyper-V container могут стать альтернативным worker-ом после включения виртуализации. Нужны отдельная минимальная среда, отключённый automount хоста, отсутствие наследуемых Windows credentials, network policy, закреплённый Python/Node runtime и проверка `\\wsl$`/`/mnt` escape. Сейчас проверять эти свойства не на чем.

## Минимальный контракт будущего backend-а

`SANDBOX_OFFLINE` принимается только если живые отрицательные тесты подтверждают одновременно:

1. единственный staging workspace доступен RW;
2. runtime доступен только RO, прочие пользовательские файлы и credentials недоступны;
3. DNS, loopback, LAN и Internet запрещены;
4. shell/UI/clipboard/input injection недоступны;
5. весь process tree остаётся в Job/VM и уничтожается при timeout/cancel;
6. применяются ограничения RAM, CPU, wall time и количества процессов;
7. среда очищена от пользовательских secrets и опасных inherited handles;
8. stdout/stderr ограничены и возвращаются по узкому протоколу;
9. symlink, junction и reparse-point escape отклоняются;
10. любой отсутствующий capability или сбой setup приводит к отказу до запуска payload.

`SANDBOX_NETWORKED` использует тот же контракт, но получает отдельное подтверждение и сеть только через контролируемый allowlist/proxy. Простое `network enabled` этим профилем не считается.

## Решение

На этом checkpoint код command backend не меняется: безопаснее сохранить доказанный fail-closed режим, чем добавить непроверенную оболочку. Следующий системный шаг требует участия Александра: включить AMD-V/SVM в UEFI. После этого повторить capability probe и сравнить живым fault-injection набором Windows Sandbox/Hyper-V worker с нативным AppContainer launcher. Экспериментальный API или MXC можно использовать только в отдельном исследовательском профиле, не в обычном запуске Ксении.

Источники Microsoft:

- [Application isolation](https://learn.microsoft.com/windows/security/book/application-security-application-isolation)
- [Launch an AppContainer](https://learn.microsoft.com/windows/win32/secauthz/implementing-an-appcontainer)
- [Create Process in Sandbox — experimental](https://learn.microsoft.com/windows/win32/secauthz/createprocessinsandbox)
- [Job Objects](https://learn.microsoft.com/windows/win32/procthread/job-objects)
- [Install Windows Sandbox](https://learn.microsoft.com/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-install)

