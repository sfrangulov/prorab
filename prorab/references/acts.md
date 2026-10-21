# Тексты актов и сообщений бригаде

Скрипты берут первый блок кода под заголовком раздела. Заголовки не переименовывать.

## Handover request

```
Заверши текущий шаг. Новых долгих прогонов не начинай. Напиши акт пересменки с полями: цель наряда; сделано (коммиты); в процессе (фоновые задачи, где их вывод, как проверить); следующие шаги; договорённости (смета, допуски, ограничения); важные пути; открытые вопросы. Когда акт написан, ответь строкой HANDOVER READY.
```

## Pause

```
Заверши текущий шаг, ничего нового не начинай, жди сообщения прораба.
```

## Customer decision to drop the branch

Прораб пишет этот акт `ответ` только после явных слов заказчика об отказе от ветки наряда. Только строка с этим маркером в начале разрешает `cleanup` удалить ветку с неслитыми коммитами.

```
[customer decision] drop
```

## Message by file

```
Read the file {path} and do what it says.
```

## Continue

```
Continue from where you stopped.
```

## Fix the report

```
The stage report failed the check; the mismatches are listed in the file {path}. Fix the report or the work and send the report again.
```
