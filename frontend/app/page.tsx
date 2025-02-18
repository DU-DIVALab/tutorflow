"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  LiveKitRoom,
  useVoiceAssistant,
  BarVisualizer,
  RoomAudioRenderer,
  VoiceAssistantControlBar,
  AgentState,
  DisconnectButton,
  useLocalParticipant,
} from "@livekit/components-react";
import { useCallback, useEffect, useState } from "react";
import { MediaDeviceFailure } from "livekit-client";
import type { ConnectionDetails } from "./api/connection-details/route";
import { NoAgentNotification } from "@/components/NoAgentNotification";
import { CloseIcon } from "@/components/CloseIcon";
import { useKrispNoiseFilter } from "@livekit/components-react/krisp";
import { Hand } from "lucide-react";

const VALID_CODES = {
  SQUARE: "User Led Interaction",
  CIRCLE: "Agent Led Interaction",
  TRIANGLE: "User Raise Hand"
};

export default function Page() {
  const [connectionDetails, updateConnectionDetails] = useState<ConnectionDetails | undefined>(undefined);
  const [agentState, setAgentState] = useState<AgentState>("disconnected");
  const [code, setCode] = useState("");
  const [mode, setMode] = useState("");
  const [showCodeEntry, setShowCodeEntry] = useState(true);
  const [isHandRaised, setIsHandRaised] = useState(false);

  const handleCodeSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const upperCode = code.toUpperCase();
    if (VALID_CODES[upperCode as keyof typeof VALID_CODES]) {
      setMode(VALID_CODES[upperCode as keyof typeof VALID_CODES]);
      setShowCodeEntry(false);
    } else {
      alert("Invalid code. Please enter SQUARE, CIRCLE, or TRIANGLE.");
    }
  };

  const onConnectButtonClicked = useCallback(async () => {
    const url = new URL(
      process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT ?? "/api/connection-details",
      window.location.origin
    );
    const response = await fetch(url.toString());
    const connectionDetailsData = await response.json();
    updateConnectionDetails(connectionDetailsData);
  }, []);

  return (
    <main data-lk-theme="default" className="h-full grid content-center bg-[var(--lk-bg)]">
      {showCodeEntry ? (
        <motion.div 
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="p-6 bg-white rounded-lg shadow-lg max-w-md mx-auto"
        >
          <form onSubmit={handleCodeSubmit} className="space-y-4">
            <input
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="Enter code (SQUARE/CIRCLE/TRIANGLE)"
              className="w-full p-2 border rounded"
            />
            <button type="submit" className="w-full p-2 bg-blue-500 text-white rounded">
              Submit
            </button>
          </form>
        </motion.div>
      ) : (
        <>
          <div className="text-center text-white text-xl mb-4">{mode}</div>
          <LiveKitRoom
            token={connectionDetails?.participantToken}
            serverUrl={connectionDetails?.serverUrl}
            connect={connectionDetails !== undefined}
            audio={true}
            video={false}
            onMediaDeviceFailure={onDeviceFailure}
            onDisconnected={() => {
              updateConnectionDetails(undefined);
            }}
            className="grid grid-rows-[2fr_1fr] items-center"
          >
            <SimpleVoiceAssistant 
              onStateChange={setAgentState} 
              isHandRaised={isHandRaised}
              mode={mode}
            />
            <ControlBar
              onConnectButtonClicked={onConnectButtonClicked}
              agentState={agentState}
              mode={mode}
              isHandRaised={isHandRaised}
              setIsHandRaised={setIsHandRaised}
            />
            <RoomAudioRenderer />
            <NoAgentNotification state={agentState} />
          </LiveKitRoom>
        </>
      )}
    </main>
  );
}

function SimpleVoiceAssistant(props: {
  onStateChange: (state: AgentState) => void;
  isHandRaised: boolean;
  mode: string;
}) {
  const { state, audioTrack } = useVoiceAssistant();
  const { microphoneTrack } = useLocalParticipant();
  
  useEffect(() => {
    props.onStateChange(state);
  }, [props, state]);

  // Control microphone based on hand raise state
  useEffect(() => {
    if (props.mode === "User Raise Hand" && microphoneTrack?.track) {
      if (props.isHandRaised) {
        microphoneTrack.track.enable();
      } else {
        microphoneTrack.track.disable();
      }
    }
  }, [props.isHandRaised, props.mode, microphoneTrack]);

  return (
    <div className="h-[300px] max-w-[90vw] mx-auto">
      <BarVisualizer
        state={state}
        barCount={5}
        trackRef={audioTrack}
        className="agent-visualizer"
        options={{ minHeight: 24 }}
      />
    </div>
  );
}

function ControlBar(props: {
  onConnectButtonClicked: () => void;
  agentState: AgentState;
  mode: string;
  isHandRaised: boolean;
  setIsHandRaised: (raised: boolean) => void;
}) {
  const krisp = useKrispNoiseFilter();
  const { localParticipant } = useLocalParticipant();

  useEffect(() => {
    krisp.setNoiseFilterEnabled(true);
  }, []);

  // Handle hand raise toggle
  const toggleHand = useCallback(() => {
    props.setIsHandRaised(!props.isHandRaised);
  }, [props.isHandRaised, props.setIsHandRaised]);

  return (
    <div className="relative h-[100px]">
      <AnimatePresence>
        {props.agentState === "disconnected" && (
          <motion.button
            initial={{ opacity: 0, top: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0, top: "-10px" }}
            transition={{ duration: 1, ease: [0.09, 1.04, 0.245, 1.055] }}
            className="uppercase absolute left-1/2 -translate-x-1/2 px-4 py-2 bg-white text-black rounded-md"
            onClick={() => props.onConnectButtonClicked()}
          >
            Start a conversation
          </motion.button>
        )}
      </AnimatePresence>
      <AnimatePresence>
        {props.agentState !== "disconnected" && props.agentState !== "connecting" && (
          <motion.div
            initial={{ opacity: 0, top: "10px" }}
            animate={{ opacity: 1, top: 0 }}
            exit={{ opacity: 0, top: "-10px" }}
            transition={{ duration: 0.4, ease: [0.09, 1.04, 0.245, 1.055] }}
            className="flex h-8 absolute left-1/2 -translate-x-1/2 justify-center items-center gap-4"
          >
            {props.mode === "User Raise Hand" && (
              <button
                onClick={toggleHand}
                className={`p-2 rounded transition-colors ${props.isHandRaised ? 'bg-green-500' : 'bg-gray-500'}`}
                title={props.isHandRaised ? "Lower Hand" : "Raise Hand"}
              >
                <Hand className="w-5 h-5 text-white" />
              </button>
            )}
            <VoiceAssistantControlBar controls={{ leave: false }} />
            <DisconnectButton>
              <CloseIcon />
            </DisconnectButton>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function onDeviceFailure(error?: MediaDeviceFailure) {
  console.error(error);
  alert(
    "Error acquiring camera or microphone permissions. Please make sure you grant the necessary permissions in your browser and reload the tab"
  );
}