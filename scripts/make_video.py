import cv2
import os


def write_video(video_name, image_folder):
    images = [img for img in os.listdir(image_folder) if img.endswith(".png")]
    frame = cv2.imread(os.path.join(image_folder, images[0]))
    height, width, layers = frame.shape

    video = cv2.VideoWriter(video_name, 0, 5, (width, height))

    for image in images:
        video.write(cv2.imread(os.path.join(image_folder, image)))

    cv2.destroyAllWindows()
    video.release()


write_video('video1.avi', '../outputs/txt2img-samples/process')
write_video('video2.avi', '../outputs/txt2img-samples/process2')
