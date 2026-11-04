"""Real-engine tests in a temporary copy. Run: python tests/test_credit_reroll.py."""
import sys, shutil, tempfile, pathlib, atexit
root=pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root/'python'))
from v3_client import V3Client, Frame
def main():
    tmp_obj=tempfile.TemporaryDirectory(prefix='xycredit_test_'); atexit.register(tmp_obj.cleanup); game=pathlib.Path(tmp_obj.name)/'game'; shutil.copytree(root/'game',game)
    p=game/'rl_v3_driver.gd'; s=p.read_text(encoding='utf-8')
    s=s.replace('var active = false','var test_selected = {}\n\nfunc _test_count(main, t):\n\tvar n = 0\n\tfor r in main.get_node("Reels").reels:\n\t\tfor ic in r.icons:\n\t\t\tif is_instance_valid(ic) and str(ic.type) == t:\n\t\t\t\tn += 1\n\t\tfor q in r.icon_types_to_be_added:\n\t\t\tif str(q) == t:\n\t\t\t\tn += 1\n\treturn n\n\nvar active = false')
    s=s.replace('\t\t\t\thover.press()','\t\t\t\tvar count_before = _test_count(main, reply)\n\t\t\t\thover.press()\n\t\t\t\ttest_selected = {"name": reply, "before": count_before, "after": _test_count(main, reply)}', 1)
    s=s.replace('"type": "frame", "v": FRAME_VERSION,','"type": "frame", "v": FRAME_VERSION,')
    anchor='"seed": rl_seed, "floor": floor_num, "actual_floor": int(popup.current_floor), "spins": popup.spins,'
    assert s.count(anchor)==1
    s=s.replace(anchor, '"test_selected": test_selected, ' + anchor)
    s=s.replace('\tif kind == "probe_removal":','\tif kind == "test_reroll":\n\t\tvar pp = main.get_node("Pop-up Sprite/Pop-up")\n\t\tpp.reroll_tokens = int(cmd.get("tokens", 1))\n\t\tpp.reroll_cost = int(cmd.get("cost", 1))\n\t\tpp.add_buttons()\n\t\tcand_list = _build_candidates(main, pp)\n\t\t_emit(main)\n\t\treturn\n\tif kind == "probe_removal":')
    p.write_text(s,encoding='utf-8')
    def cards(c,f): return [(c.type_name(x.type_id),x) for x in f.candidates if x.kind==0]
    def prep(c,item):
     f=c.reset('CREDITTEST01',1)
     c._send({'cmd':'probe_grant','item':item,'saved':6}); g=c._recv(); assert g['ok']; c.frame=Frame.decode(c._recv(),c.hello['shapes']); f=c.frame
     a=next(x for x in f.candidates if x.kind==0 and c.type_name(x.type_id)=='anchor'); f=c.step(a.index)
     if not f.info['email']:
      f=c.step(next(x.index for x in f.candidates if x.type_id==430))
     assert f.info['email']=='add_tile_prompt',f.info
     return f
    with V3Client(game=game,engine=root/'runtime/godot.exe',timeout=50) as c:
     for item in ['credit_card','credit_card_essence']:
      f=prep(c,item); names=[n for n,x in cards(c,f)]; print(item,'COUNT',len(names),'NAMES',names,flush=True); assert len(names)==(20 if item=='credit_card' else 30) and len(names)==len(set(names)); owned=[c.type_name(t) for t,m in zip(f.item_types,f.item_mask) if m]; assert (item in owned)==(item=='credit_card'),owned
      for name in names:
       f=prep(c,item); opts=dict(cards(c,f)); assert name in opts; before=f.info['deck_len']; f=c.step(opts[name].index); d=f.info['test_selected']; assert d['name']==name and d['after']==d['before']+1,(item,name,d); assert f.info['unresolved']==0,(item,name,f.info); print('chosen',item,name,d,flush=True)
      f=prep(c,item); f=c.command({'cmd':'test_reroll','tokens':3,'cost':2}); pre=[n for n,x in cards(c,f)]; rer=next(x for x in f.candidates if x.type_id==414); f=c.step(rer.index); post=[n for n,x in cards(c,f)]; print('REROLL',item,'pre',pre,'post',post,'tokens',f.info['reroll_tokens'],flush=True); assert f.info['reroll_tokens']==1 and pre!=post and len(post)>3; choice=cards(c,f)[-1]; f=c.step(choice[1].index); assert f.info['test_selected']['name']==choice[0] and f.info['test_selected']['after']==f.info['test_selected']['before']+1
     f=c.reset('REROLLTEST1',1); f=c.command({'cmd':'test_reroll','tokens':2,'cost':1}); pre=[n for n,x in cards(c,f)]; f=c.step(next(x.index for x in f.candidates if x.type_id==414)); post=[n for n,x in cards(c,f)]; print('NORMAL REROLL',pre,post,f.info['reroll_tokens'],flush=True); assert f.info['reroll_tokens']==1 and pre!=post; f=c.step(cards(c,f)[0][1].index); assert not f.terminated
     print('PASS',flush=True)


if __name__ == "__main__":
    main()
