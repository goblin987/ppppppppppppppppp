# -*- coding: utf-8 -*-
import os

# Mapping of corrupted sequences to proper emojis
replacements = {
    # Common emojis
    b'\xc3\xb0\xc2\x9f\xc2\x91\xc2\x8b': '👋'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x91\xc2\xa4': '👤'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x92\xc2\xb0': '💰'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\xa6': '📦'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x9b\xc2\x92': '🛒'.encode('utf-8'),
    b'\xc3\xa2\xc2\x9a\xc2\xa0\xc3\xaf\xc2\xb8\xc2\x8f': '⚠️'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x94\xc2\xa7': '🔧'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x8e\xc2\x89': '🎉'.encode('utf-8'),
    b'\xc3\xa2\xc2\x9c\xc2\x85': '✅'.encode('utf-8'),
    b'\xc3\xa2\xc2\x9d\xc2\x8c': '❌'.encode('utf-8'),
    b'\xc3\xa2\xc2\xac\xc2\x85\xc3\xaf\xc2\xb8\xc2\x8f': '⬅️'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x94\xc2\x8d': '🔍'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\x8a': '📊'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\x8b': '📋'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\x9d': '📝'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x86\xc2\x94': '🆔'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x8f\xc2\xa0': '🏠'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x8c\xc2\x90': '🌐'.encode('utf-8'),
    b'\xc3\xa2\xc2\xad\xc2\x90': '⭐'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x94\xc2\xa5': '🔥'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x92\xc2\x8e': '💎'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x92\xc2\xb3': '💳'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x8f\xc2\xb7\xc3\xaf\xc2\xb8\xc2\x8f': '🏷️'.encode('utf-8'),
    b'\xc3\xa2\xc2\x8f\xc2\xa9': '⏩'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x94\xc2\x84': '🔄'.encode('utf-8'),
    b'\xc3\xa2\xc2\x9d\xc2\xa4\xc3\xaf\xc2\xb8\xc2\x8f': '❤️'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\x9c': '📜'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x92\xc2\xac': '💬'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\xa2': '📢'.encode('utf-8'),
    b'\xc3\xa2\xc2\x9c\xc2\x8f\xc3\xaf\xc2\xb8\xc2\x8f': '✏️'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\xa7\xc2\xb9': '🧹'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\x81': '📁'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x94\xc2\x90': '🔐'.encode('utf-8'),
    b'\xc3\xa2\xc2\x8f\xc2\xb1\xc3\xaf\xc2\xb8\xc2\x8f': '⏱️'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x9a\xc2\xab': '🚫'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x93\xc2\x85': '📅'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x94\xc2\x80': '🔀'.encode('utf-8'),
    b'\xc3\xb0\xc2\x9f\xc2\x98\xc2\xb6\xe2\x80\x8d\xc3\xb0\xc2\x9f\xc2\x8c\xc2\xab\xc3\xaf\xc2\xb8\xc2\x8f': '😶‍🌫️'.encode('utf-8'),
}

for filename in os.listdir('.'):
    if filename.endswith('.py') and filename != 'fix_encoding.py':
        try:
            with open(filename, 'rb') as f:
                content = f.read()
            
            # Remove BOM if present
            if content.startswith(b'\xef\xbb\xbf'):
                content = content[3:]
            
            original = content
            for bad, good in replacements.items():
                content = content.replace(bad, good)
            
            if content != original:
                with open(filename, 'wb') as f:
                    f.write(content)
                print(f'Fixed: {filename}')
            else:
                print(f'No changes: {filename}')
        except Exception as e:
            print(f'Error: {filename} - {e}')

print('Done!')

