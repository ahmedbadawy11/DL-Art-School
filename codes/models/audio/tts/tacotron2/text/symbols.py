""" from https://github.com/keithito/tacotron """

'''
Defines the set of symbols used in text input to the model.

The default is a set of ASCII characters that works well for English or text that has been run through Unidecode. For other data, you can modify _characters. See TRAINING_DATA.md for details. '''
from models.audio.tts.tacotron2.text import cmudict

_pad        = '_'
_punctuation = '!\'(),.:;? '
_special = '-'
# _letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
_letters = '''
أَ أُ إِ أَّ أُّ أِّ أْ
بَ بُ بِ بَّ بُّ بِّ بْ
تَ تُ تِ تَّ تُّ تِّ تْ
ثَ ثُ ثِ ثَّ ثُّ ثِّ ثْ
جَ جُ جِ جَّ جُّ جِّ جْ
حَ حُ حِ حَّ حُّ حِّ حْ
خَ خُ خِ خَّ خُّ خِّ خْ
دَ دُ دِ دَّ دُّ دِّ دْ
ذَ ذُ ذِ ذَّ ذُّ ذِّ ذْ
رَ رُ رِ رَّ رُّ رِّ رْ
زَ زُ زِ زَّ زُّ زِّ زْ
سَ سُ سِ سَّ سُّ سِّ سْ
شَ شُ شِ شَّ شُّ شِّ شْ
صَ صُ صِ صَّ صُّ صِّ صْ
ضَ ضُ ضِ ضَّ ضُّ ضِّ ضْ
طَ طُ طِ طَّ طُّ طِّ طْ
ظَ ظُ ظِ ظَّ ظُّ ظِّ ظْ
عَ عُ عِ عَّ عُّ عِّ عْ
غَ غُ غِ غَّ غُّ غِّ غْ
فَ فُ فِ فَّ فُّ فِّ فْ
قَ قُ قِ قَّ قُّ قِّ قْ
كَ كُ كِ كَّ كُّ كِّ كْ
لَ لُ لِ لَّ لُّ لِّ لْ
مَ مُ مِ مَّ مُّ مِّ مْ
نَ نُ نِ نَّ نُّ نِّ نْ
هَ هُ هِ هَّ هُّ هِّ هْ
وَ وُ وِ وَّ وُّ وِّ وْ
يَ يُ يِ يَّ يُّ يِّ يْ
أ ا ب ت ث ج ح خ د ذ ر ز س ش ص ض ط ظ ع غ ف ق ك ل م ن هـ و ي
'''
# Prepend "@" to ARPAbet symbols to ensure uniqueness (some are the same as uppercase letters):
_arpabet = ['@' + s for s in cmudict.valid_symbols]

# Export all symbols:
symbols = [_pad] + list(_special) + list(_punctuation) + list(_letters) + _arpabet
