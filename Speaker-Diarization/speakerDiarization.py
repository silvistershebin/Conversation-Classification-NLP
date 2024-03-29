
"""A demo script showing how to DIARIZATION ON WAV USING UIS-RNN."""

import numpy as np
import uisrnn
import librosa
import sys
sys.path.append('ghostvlad')
sys.path.append('visualization')
import toolkits
from ghostvlad import model as spkModel
import os
import speech_recognition as sr

# ===========================================
#        Parse the argument
# ===========================================
import argparse
parser = argparse.ArgumentParser()
# set up training configuration.
parser.add_argument('--gpu', default='', type=str)
parser.add_argument('--resume', default=r'/home/shebin/Desktop/COLAB/Speaker-Diarization/ghostvlad/pretrained/weights.h5', type=str)
parser.add_argument('--data_path', default='4persons', type=str)
# set up network configuration.
parser.add_argument('--net', default='resnet34s', choices=['resnet34s', 'resnet34l'], type=str)
parser.add_argument('--ghost_cluster', default=2, type=int)
parser.add_argument('--vlad_cluster', default=8, type=int)
parser.add_argument('--bottleneck_dim', default=512, type=int)
parser.add_argument('--aggregation_mode', default='gvlad', choices=['avg', 'vlad', 'gvlad'], type=str)
# set up learning rate, training loss and optimizer.
parser.add_argument('--loss', default='softmax', choices=['softmax', 'amsoftmax'], type=str)
parser.add_argument('--test_type', default='normal', choices=['normal', 'hard', 'extend'], type=str)

global args
args = parser.parse_args()


SAVED_MODEL_NAME = '/home/shebin/Desktop/COLAB/Speaker-Diarization/pretrained/saved_model.uisrnn_benchmark'

def append2dict(speakerSlice, spk_period):
    key = list(spk_period.keys())[0]
    value = list(spk_period.values())[0]
    timeDict = {}
    timeDict['start'] = int(value[0]+0.5)
    timeDict['stop'] = int(value[1]+0.5)
    if(key in speakerSlice):
        speakerSlice[key].append(timeDict)
    else:
        speakerSlice[key] = [timeDict]

    return speakerSlice

def arrangeResult(labels, time_spec_rate): # {'1': [{'start':10, 'stop':20}, {'start':30, 'stop':40}], '2': [{'start':90, 'stop':100}]}
    lastLabel = labels[0]
    speakerSlice = {}
    j = 0
    for i,label in enumerate(labels):
        if(label==lastLabel):
            continue
        speakerSlice = append2dict(speakerSlice, {lastLabel: (time_spec_rate*j,time_spec_rate*i)})
        j = i
        lastLabel = label
    speakerSlice = append2dict(speakerSlice, {lastLabel: (time_spec_rate*j,time_spec_rate*(len(labels)))})
    return speakerSlice

def genMap(intervals):  # interval slices to maptable
    slicelen = [sliced[1]-sliced[0] for sliced in intervals.tolist()]
    mapTable = {}  # vad erased time to origin time, only split points
    idx = 0
    for i, sliced in enumerate(intervals.tolist()):
        mapTable[idx] = sliced[0]
        idx += slicelen[i]
    mapTable[sum(slicelen)] = intervals[-1,-1]

    keys = [k for k,_ in mapTable.items()]
    keys.sort()
    return mapTable, keys

def fmtTime(timeInMillisecond):
    millisecond = timeInMillisecond%1000
    minute = timeInMillisecond//1000//60
    second = (timeInMillisecond-minute*60*1000)//1000
    time = '{}:{:02d}.{}'.format(minute, second, millisecond)
    return time

def load_wav(vid_path, sr):
    wav, _ = librosa.load(vid_path, sr=sr)
    intervals = librosa.effects.split(wav, top_db=20)
    wav_output = []
    for sliced in intervals:
      wav_output.extend(wav[sliced[0]:sliced[1]])
    return np.array(wav_output), (intervals/sr*1000).astype(int)

def lin_spectogram_from_wav(wav, hop_length, win_length, n_fft=1024):
    linear = librosa.stft(wav, n_fft=n_fft, win_length=win_length, hop_length=hop_length) # linear spectrogram
    return linear.T


# 0s        1s        2s                  4s                  6s
# |-------------------|-------------------|-------------------|
# |-------------------|
#           |-------------------|
#                     |-------------------|
#                               |-------------------|
def load_data(path, win_length=400, sr=16000, hop_length=160, n_fft=512, embedding_per_second=0.5, overlap_rate=0.5):
    wav, intervals = load_wav(path, sr=sr)
    linear_spect = lin_spectogram_from_wav(wav, hop_length, win_length, n_fft)
    mag, _ = librosa.magphase(linear_spect)  # magnitude
    mag_T = mag.T
    freq, time = mag_T.shape
    spec_mag = mag_T

    spec_len = sr/hop_length/embedding_per_second
    spec_hop_len = spec_len*(1-overlap_rate)

    cur_slide = 0.0
    utterances_spec = []

    while(True):  # slide window.
        if(cur_slide + spec_len > time):
            break
        spec_mag = mag_T[:, int(cur_slide+0.5) : int(cur_slide+spec_len+0.5)]
        
        # preprocessing, subtract mean, divided by time-wise var
        mu = np.mean(spec_mag, 0, keepdims=True)
        std = np.std(spec_mag, 0, keepdims=True)
        spec_mag = (spec_mag - mu) / (std + 1e-5)
        utterances_spec.append(spec_mag)

        cur_slide += spec_hop_len

    return utterances_spec, intervals

def main(wav_path, embedding_per_second=1.0, overlap_rate=0.5):

    # gpu configuration
    toolkits.initialize_GPU(args)

    params = {'dim': (257, None, 1),
              'nfft': 512,
              'spec_len': 250,
              'win_length': 400,
              'hop_length': 160,
              'n_classes': 5994,
              'sampling_rate': 16000,
              'normalize': True,
              }

    network_eval = spkModel.vggvox_resnet2d_icassp(input_dim=params['dim'],
                                                num_class=params['n_classes'],
                                                mode='eval', args=args)
    network_eval.load_weights(args.resume, by_name=True)


    model_args, _, inference_args = uisrnn.parse_arguments()
    model_args.observation_dim = 512
    uisrnnModel = uisrnn.UISRNN(model_args)
    uisrnnModel.load(SAVED_MODEL_NAME)

    specs, intervals = load_data(wav_path, embedding_per_second=embedding_per_second, overlap_rate=overlap_rate)
    mapTable, keys = genMap(intervals)

    feats = []
    for spec in specs:
        spec = np.expand_dims(np.expand_dims(spec, 0), -1)
        v = network_eval.predict(spec)
        feats += [v]

    feats = np.array(feats)[:,0,:].astype(float)  # [splits, embedding dim]
    predicted_label = uisrnnModel.predict(feats, inference_args)

    time_spec_rate = 1000*(1.0/embedding_per_second)*(1.0-overlap_rate) # speaker embedding every ?ms
    center_duration = int(1000*(1.0/embedding_per_second)//2)
    speakerSlice = arrangeResult(predicted_label, time_spec_rate)

    for spk,timeDicts in speakerSlice.items():    # time map to orgin wav(contains mute)
        for tid,timeDict in enumerate(timeDicts):
            s = 0
            e = 0
            for i,key in enumerate(keys):
                if(s!=0 and e!=0):
                    break
                if(s==0 and key>timeDict['start']):
                    offset = timeDict['start'] - keys[i-1]
                    s = mapTable[keys[i-1]] + offset
                if(e==0 and key>timeDict['stop']):
                    offset = timeDict['stop'] - keys[i-1]
                    e = mapTable[keys[i-1]] + offset

            speakerSlice[spk][tid]['start'] = s
            speakerSlice[spk][tid]['stop'] = e
    file1 = open("myfile.txt","w+")
    for spk,timeDicts in speakerSlice.items():
        file1.write("person"+"\n")
        for timeDict in timeDicts:
            s = timeDict['start']
            e = timeDict['stop']
            s = fmtTime(s)  # change point moves to the center of the slice
            e = fmtTime(e)
            print(s+' ==> '+e)
            filestart = s +" -- "
            fileend = e +"\n"
            file1.write(filestart)
            file1.write(fileend)
    file1.close()



if __name__ == '__main__':
	import shutil
	src_files = os.listdir('Speaker-Diarization/wavs/')
	for file_name in src_files:
		try:
			os.remove(os.path.join('Speaker-Diarization/wavs/', "rmdmy.wav"))
		except:
			no = 0
		full_file_name = os.path.join('Speaker-Diarization/wavs/', file_name)
		os.rename(full_file_name,os.path.join('Speaker-Diarization/wavs/', 'rmdmy.wav'))
		print('\n\n\n\n\n\nFILE: ',full_file_name)
		main('/home/shebin/Desktop/COLAB/Speaker-Diarization/wavs/rmdmy.wav', embedding_per_second=1.2, overlap_rate=0.4)
		try:
			shutil.rmtree("chunks")
		except:
			n=0
		os.mkdir('chunks')



		from pydub import AudioSegment
		f = open("Speaker-Diarization/myfile.txt", "r")
		i = 0
		flag = 0
		for x in f:
			flag = 0
			x = str(x)
			if (x == "person\n"):
				donothing = 0
			else :
				flag = 1
				a, b = x.split(' -- ')# a == Start# b == End
				minutes1, secmill1 = a.split(":")
				sec1, milli1 = secmill1.split(".")# print("min", minutes1, "sec", sec1, "milli", milli1)
				minutes1 = int(minutes1)
				sec1 = int(sec1)
				milli1 = int(milli1)
				milliseconds1 = ((minutes1 * 60 * 1000) + (sec1 * 1000) + milli1)
			
				minutes2, secmill2 = b.split(":")
				sec2, milli2 = secmill2.split(".")# print("min", minutes2, "sec", sec2, "milli", milli2)
				minutes2 = int(minutes2)
				sec2 = int(sec2)
				milli2 = int(milli2)
				milliseconds2 = ((minutes2 * 60 * 1000) + (sec2 * 1000) + milli2)# print(milliseconds1, " --> ", milliseconds2)
				i = i + 1
				newAudio = AudioSegment.from_wav(r'Speaker-Diarization/wavs/rmdmy.wav')
				newAudio = newAudio[milliseconds1: milliseconds2]
				name = ('chunks/chunked{0}.wav').format(i)
				newAudio.export(name, format = "wav")
				numoffiles = i
		
				# Code to recognize
		if (flag==1):
			r = sr.Recognizer()
			file2 = open("Speaker-Diarization/speechtotext.txt", "w+")
			for ii in range(1, numoffiles):
				name = os.path.join('chunks/', ('chunked{0}.wav').format(ii))
				harvard = sr.AudioFile(name)
				with harvard as source:
					audio = r.record(source)
				type(audio)
				try:
					text = r.recognize_google(audio)
					text = text + "\n"
					file2.write(text)
				except: #do nothing
					donothing = 0


			file2.close()
