import sys, os
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, S + "/snap/core"); sys.path.insert(0, S + "/snap/tests")
import logging; logging.disable(logging.CRITICAL)
import conftest, core, speech_norm
def show(fn, xs):
    print(f"--- {fn.__name__}")
    for x in xs:
        y = fn(x)
        print(("   " if y == x else " * ") + repr(x) + "  ->  " + repr(y))
show(core.fix_english_numbers, [
 "Включаю Twenty One Pilots — Stressed Out.", "Играет Nine Inch Nails.", "Metallica — One, отличная вещь.",
 "Seven Nation Army от The White Stripes.", "Three Days Grace — Never Too Late.", "Thirty Seconds to Mars",
 "Dave Brubeck — Take Five", "Five Finger Death Punch", "Pearl Jam, альбом Ten", "Beatles — Eight Days a Week",
 "Ground Zero", "плюс thirteen", "plus seventeen градусов", "twenty-one", "one hundred twenty", "It's one of the best",
 "Taylor Swift — seven", "Fifty Shades", "Qwen3 one-shot", "Окей, one moment",
])
show(core.feminine, [
 "Я понял.", "Я рад тебя слышать.", "Я сам могу включить.", "Сам могу включить.", "Он сказал: я сам видел.",
 "Ты говоришь «я устал» — давай отдохнём.", "Я один раз там была.", "Я тоже футбол люблю.", "Я сериал досмотрела.",
 "Я канал переключила.", "Я сигнал услышала.", "Я Байкал видела только на фото.", "Я тебе материал нашла.",
 "Я ошибся.", "Я заблудился в меню.", "Я, честно говоря, не понял.", "Я тебе так и не сказал.", "Я бы тоже хотел.",
 "Я принёс новости.", "Я его не видел.", "Я нашёл.", "Я прочёл.", "Я начал.", "Я ж говорил.", "я одна осталась",
 "Я спал.", "Я тебе звонил.", "Я тоже металл слушаю.", "Я Павел.", "Я идеал.", "Я тоже Урал люблю",
])
show(speech_norm.normalize, [
 "Сейчас +14°, ветер 4 м/с.", "Ночью до –5.", "Ночью до −5.", "Ночью до -5.", "Ночью –3°.", "Будет «+14».",
 "Днём +12…+14°.", "Днём 12-14°.", "Днём от +12 до +14 °C.", "Осадков 2 мм.", "Осадков 21 мм.", "около 1,5 мм",
 "с 2 мм до 5 мм", "Встреча в 10:30.", "Это 1-й раз.", "Счёт 2 -1 в нашу пользу.", "Звони 8-800-555-35-35 или +7 999 123-45-67.",
 "Скидка -20% на всё.", "Модель Qwen3.8-27B и GPT-5.", "COVID-19 и MP3, 4K.", "версия 2.0 -10 багов", "в 1990 г. и 2000 гг.",
 "Это было в 44 г. до н. э.", "С самого начала было ясно.", "До начала фильма 5 минут.", "Она начала петь.",
 "давление 745 мм рт. ст.", "5 г. сахара", "5 см.", "Мм, вкусно.", "1 000 мм", "температура 36,6°", "-3,5°C",
 "x-5", "(−7°)", "https://ya.ru/?q=-5 и 2 мм", "10-15 мм", "до 3 мм-х",
])
print("--- clean_for_speech")
for x in ["[teasing] Ну ты даёшь. [teasing] Опять!", "Ну конечно. [teasing] Опять ты за своё.", "[excited] Ура! [laughing] Ха-ха.",
          "Смотри: https://habr.com/ru/articles/1 и habr.com", "Погода: +14°C, 2 мм. Я понял, twenty one.",
          "- первый пункт\n- второй", "Курс ~95 руб. > 90", "Я сам видел [Глава 1]", "Привет 😀✔ ★"]:
    print(" ", repr(x), "->", repr(core.clean_for_speech(x)))
print("--- split_tail_offer")
for x in ["Готово. Хочешь ещё?", "Играет рок. Продолжим?", "Вот так. Ещё?", "Вот так. Включить что-нибудь ещё?",
          "Это сложный вопрос. Как тебе такая идея?", "Нашла три версии. Какую включить?", "Готово. Хочешь, расскажу ещё?»",
          "Глава кончилась. Дальше читать?", "Ну вот. Интересно, правда?"]:
    print(" ", repr(x), "->", core.split_tail_offer(x))
