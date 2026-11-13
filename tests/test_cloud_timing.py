import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import analyze, audio, config, diarize, pipeline, speakers


def test_packet_boundaries_from_real_mp3_are_used(tmp_path):
    if not audio.ffmpeg_available(): pytest.skip('ffmpeg required for media integration check')
    source = tmp_path/'tone.mp3'
    audio._run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=16000',
                '-t','5.3','-c:a','libmp3lame','-b:a','64k',str(source)])
    parts = audio.split_audio_timed(source,tmp_path/'parts',2)
    assert len(parts) == 3
    offsets = [offset for offset,_ in parts]
    def packets(path):
        return json.loads(audio._run(['ffprobe','-v','error','-show_packets','-show_data_hash','sha256',
            '-show_entries','packet=pts_time,duration_time,data_hash','-of','json',str(path)]))['packets']
    original = packets(source)
    cursor = 0
    # Independent ffprobe packet hashes prove that stream copy kept every frame
    # and that the offsets refer to their actual positions in the source.
    for offset, path in parts:
        chunk = packets(path)
        assert offset == pytest.approx(float(original[cursor]['pts_time']), abs=.000001)
        assert [p['data_hash'] for p in chunk] == [p['data_hash'] for p in original[cursor:cursor+len(chunk)]]
        cursor += len(chunk)
    assert cursor == len(original)
    assert offsets[0] == 0
    assert offsets[1] != 2
    assert offsets[-1] < audio.duration_seconds(source)


def test_cloud_pipeline_offsets_do_not_overlap_or_reset_last_chunk(tmp_path,monkeypatch):
    source=tmp_path/'input.mp3'; source.write_bytes(b'large')
    monkeypatch.setattr(config,'is_local_asr',lambda model: False)
    monkeypatch.setattr(config,'is_qwen',lambda: False)
    monkeypatch.setattr(pipeline,'MAX_WHISPER_BYTES',1)
    monkeypatch.setattr(pipeline,'AUDIO_DIR',tmp_path)
    offsets=[0.,120.024,240.012]
    async def split(*args): return [(offset,Path(str(i))) for i,offset in enumerate(offsets)]
    async def transcribe(client,path,model):
        return {'text':f'Фраза {path.name}','segments':[{'start':.1,'end':.5,'text':f'Фраза {path.name}'}]}
    monkeypatch.setattr(pipeline,'split_audio_async',split)
    monkeypatch.setattr(pipeline,'transcribe_file',transcribe)
    result=asyncio.run(pipeline._transcribe(object(),source,'test',239.,SimpleNamespace(set=lambda *a,**kw:None),asr_model='whisper-1'))
    assert [item['start'] for item in result['segments']] == pytest.approx([offset+.1 for offset in offsets])
    assert [item['text'] for item in result['segments']] == ['Фраза 0','Фраза 1','Фраза 2']


def test_text_only_asr_and_mixed_fallback_keep_all_text_without_fake_times(monkeypatch):
    def forbidden(*a,**kw): pytest.fail('Diarization cannot align speech without timestamps')
    monkeypatch.setattr(diarize,'diarize_turns',forbidden)
    monkeypatch.setattr(diarize,'_unavailable_reason',forbidden)
    for chunks in ([(0, {'text':'Первый текст','segments':[]})], [
        (0,{'text':'Первая фраза','segments':[{'start':0,'end':1,'text':'Первая фраза'}]}),
        (120,{'text':'Вторая фраза без таймкодов','segments':[]})]):
        merged=analyze.merge_transcripts(chunks)
        document=diarize.apply_diarization(Path('unused.mp3'),merged)
        assert 'не вернула таймкоды' in document['note']
        for _,payload in chunks: assert payload['text'] in document['text']
        assert document['segments'][-1]['start'] is None
        assert document['segments'][-1]['speaker'] == speakers.UNKNOWN_ID
    plain=diarize.apply_diarization(Path('unused.mp3'),{'text':'Полный текст','segments':[]})
    assert 'Полный текст' in plain['text'] and '[00:00]' not in plain['text']


@pytest.mark.parametrize('start,end', [(None,None),(0,0),(-1,2),(float('nan'),2),(0,float('inf'))])
def test_invalid_provider_times_are_not_rendered_as_real_speaker_times(start,end):
    result=analyze.merge_transcripts([(3,{'text':'Текст','segments':[{'start':start,'end':end,'text':'Текст'}]})])
    assert result['segments'][0] == {'start':None,'end':None,'text':'Текст'}
