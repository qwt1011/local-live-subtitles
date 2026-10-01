# en_asmr_2600  large/small 一致度 0.900，分歧 5 处

## large-v3
[00:01.6] There we go.
[00:04.4] Good boy.
[00:08.0] Alright, well, you can have it back, and I'll be calling you soon.
[00:15.0] You better pick up, dork.
[00:18.2] Or else.
[00:22.0] Okay, catch you later.
[00:27.8] Buh-bye.
[00:32.9] Hey, but, um, before I go, though, can you do something for me?
[00:45.0] Say, what, what, did I stutter?
[00:54.9] Say it again.
[00:56.9] Puppies like to woof, don't they?

## small
[00:01.6] There we go
[00:04.3] Good boy
[00:06.1] All right. Well, you can have it back and I'll be calling you soon
[00:15.3] You better pick up dork or else
[00:21.9] Okay, catch you later
[00:27.6] Bye-bye
[00:29.1] Hey, but um
[00:35.3] Before I go though
[00:37.9] And you do something for me
[00:42.1] Say
[00:46.4] Well, what did I stutter?
[00:52.3] Say it again
[00:56.5] But bees like to woof don't they?

## SenseVoice 2024（被测对象，仅对照）
[00:01.9] There we go.
[00:04.6] Good boy.
[00:08.0] Alright.
[00:09.3] お？
[00:10.0] You can have a back.
[00:11.7] And I'll be calling you.
[00:13.6] Soon.
[00:15.5] You better pick up Do.
[00:18.1] Or else.
[00:22.1] Okay.
[00:25.6] Gotch you later.
[00:27.9] Bye bye.
[00:32.9] Hey.
[00:34.0] But.
[00:35.6] Before I go though.
[00:40.1] And you do something for me?
[00:45.1] Say.
[00:46.6] What.
[00:50.5] One did I stutter?
[00:55.0] Say it again.
[00:56.8] Puppies like to Wolf, don't they?

## 分歧（large | small | large 上下文）
- alright | all right | we go good boy 【alright】 well you can have
- buh | bye | okay catch you later 【buh】 bye hey but um
- can | and | before i go though 【can】 you do something for
- what | well | something for me say 【what】 what did i stutter
- puppies | but bees | stutter say it again 【puppies】 like to woof don't

## YouTube 自动字幕（与 large-v3 一致度 0.660，分歧 7 处）
There we go. All right. Well, you can have it back, and I'll be calling you soon. You better pick up, dork, Okay. Then, uh catch you later. Bye-bye. Hey, but um Say woof. Say it again.

## 分歧（large | YouTube | large 上下文）
- good boy alright | all right | there we go 【good boy alright】 well you can have
- or else | ∅ | better pick up dork 【or else】 okay catch you later
- ∅ | then uh | dork or else okay 【】 catch you later buh
- buh | bye | okay catch you later 【buh】 bye hey but um
- before i go though can you do something for me | ∅ | bye hey but um 【before i go though can you do something for me】 say what what did
- what what did i stutter | woof | something for me say 【what what did i stutter】 say it again puppies
- puppies like to woof don't they | ∅ | stutter say it again 【puppies like to woof don't they】
