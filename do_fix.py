import os

# Direct string replacements
replacements = [
    ("\ufffd\u0153\u2026", "\u2705"),  # check mark
    ("\ufffd\u201e\ufffd\ufe0f", "\u2139\uFE0F"),  # info
    ("\ufffd\u0178\ufffd\u02dc\ufe0f", "\U0001F3D8\uFE0F"),  # houses
    ("\ufffd\u0178\u2019\u017d", "\U0001F48E"),  # gem
    ("\ufffd\u0178\u201c\u017e", "\U0001F4DE"),  # phone
    ("\ufffd\u0178\u2019\ufffd", "\U0001F465"),  # people
    ("\ufffd\u0178\u2019\u2019", "\U0001F451"),  # crown
    ("\ufffd\u0161\u2122\ufe0f", "\u2699\uFE0F"),  # gear
    ("\ufffd\u0178\u2014\u2019\ufe0f", "\U0001F5D1\uFE0F"),  # trash
    ("\ufffd\u017e\u2022", "\u2795"),  # plus
    ("\ufffd\u0178\u201c\u201e", "\U0001F504"),  # arrows
    ("\ufffd\u0178\u2019\ufffd\ufe0f", "\U0001F441\uFE0F"),  # eye
    ("\ufffd\u0153\u2026\ufe0f", "\u2B05\uFE0F"),  # left arrow
    ("\ufffd\u0178\u0178\u2030", "\U0001F389"),  # party
    ("\ufffd\u0178\u0161\ufffd", "\U0001F680"),  # rocket
    ("\ufffd\u0178\u201c\u0160", "\U0001F4CA"),  # chart
    ("\ufffd\u0178\u201c\u2039", "\U0001F4CB"),  # clipboard
    ("\ufffd\u0178\u201c\ufffd", "\U0001F4C1"),  # folder
    ("\ufffd\u0178\u201c\u00a2", "\U0001F4E2"),  # mega
    ("\ufffd\u0178\u2019\u00ac", "\U0001F4AC"),  # speech
    ("\ufffd\u0178\u201c\u0153", "\U0001F4DC"),  # scroll
    ("\ufffd\u0178\u2019\u00b3", "\U0001F4B3"),  # credit card
    ("\ufffd\u0178\u201c\u00a5", "\U0001F525"),  # fire
    ("\ufffd\u0178\u00a7\u00b9", "\U0001F9F9"),  # broom
    ("\ufffd\u0178\u0161\u00ab", "\U0001F6AB"),  # no entry
    ("\ufffd\ufffd\u0152", "\u274C"),  # X mark
]

for fn in os.listdir('.'):
    if fn.endswith('.py') and fn != 'do_fix.py' and fn != 'fix_encoding.py':
        try:
            with open(fn, 'r', encoding='utf-8', errors='replace') as f:
                c = f.read()
            o = c
            for bad, good in replacements:
                c = c.replace(bad, good)
            if c != o:
                with open(fn, 'w', encoding='utf-8') as f:
                    f.write(c)
                print(f'Fixed: {fn}')
        except Exception as e:
            print(f'Error {fn}: {e}')

print('Done')

