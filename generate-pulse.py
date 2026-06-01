import wave
import struct
import math

filename = "ambient.wav"
sample_rate = 24000
duration = 5.0
num_samples = int(sample_rate * duration)

f1 = 165.0
f2 = 220.0  # Perfect Fourth above the base

with wave.open(filename, "w") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(sample_rate)
    
    for i in range(num_samples):
        t = i / sample_rate
        
        base_env = (math.sin(2 * math.pi * 0.25 * t - math.pi/2) + 1.0) / 2.0
        top_env = base_env ** 6
        bottom_env = 1.0 - top_env
        
        wave1 = bottom_env * math.sin(2 * math.pi * f1 * t)
        wave2 = top_env * math.sin(2 * math.pi * f2 * t)
        
        master_volume = 2000.0
        
        value = int(master_volume * (wave1 + wave2))
        if value > 32767: value = 32767
        if value < -32768: value = -32768
        
        data = struct.pack("<h", value)
        f.writeframesraw(data)