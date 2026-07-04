# CCLG demo-store

이 폴더는 설명 이미지와 문서가 가리키는 실제 CCLG 저장소 예시다.

## 레코드 흐름

1. `raw/demo-source.md`
   원본 설명 자료다.

2. `nodes/mem_0524abcafba4.json`
   오래된 가정이다. `status=superseded`라서 active memory로 주입되면 안 된다.

3. `patches/patch_ad5c337890c3.json`
   `operation=refine` 패치다. `target_ids`는 old node를, `new_node_ids`는 새
   active node를 가리킨다.

4. `edges/edge_abb6b33de5df.json`
   새 node가 old node를 `refines`했다는 관계다.

5. `nodes/mem_d6caa4a716ff.json`
   hook/MCP context로 소비되는 active node다.

6. `nodes/mem_abedb4edaeb1.json`
   `demo-session`에서만 보이는 `active_session` overlay node다.

7. `sessions/demo-session.json`
   hook event와 overlay id를 담는 session state다.

8. `active/codegraphs/CCLG.json`
   CCLG repo의 files/symbols/import edges/git metadata를 담는 code graph다.

## 검증 명령

```bash
PYTHONPATH=src python3 -m cclg.cli --root docs/explainer/demo-store doctor --json
PYTHONPATH=src python3 -m cclg.cli --root docs/explainer/demo-store pack --query "CCLG format" --format toml --session-id demo-session
PYTHONPATH=src python3 -m cclg.cli --root docs/explainer/demo-store grep "raw evidence" --json
```
