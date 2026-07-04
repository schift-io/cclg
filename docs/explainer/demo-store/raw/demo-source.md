# Demo Source

이 파일은 CCLG 설명 자료에서 raw evidence가 어떻게 저장되는지 보여주기 위한
원본 증거 파일이다.

핵심 주장:

- 원본 증거는 `raw/`에 보존한다.
- 장기 기억은 `nodes/`의 `MemoryNode`로 저장한다.
- 수정은 `patches/`의 `MemoryPatch`로 기록한다.
- 훅은 active memory와 code graph만 `additionalContext`로 반환한다.

